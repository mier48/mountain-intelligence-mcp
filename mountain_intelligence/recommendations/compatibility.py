"""Boot <-> crampon compatibility (four explicit states, evidence-backed, rule-versioned).

States:
- compatible:   a documented pair-level statement exists and no evidence contradicts it.
- incompatible: a documented incompatibility (declared types/welts/size range exclude the pair).
- conditional:  declared binding types overlap; the concrete fit still depends on size,
                adjustment and physical checks. Type compatibility is NEVER reported as
                confirmed compatibility of the specific pair.
- unknown:      missing or contradictory evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from mountain_intelligence.catalog.models import DataStatus, Product
from mountain_intelligence.catalog.queries import NotFoundError, QueryValidationError, get_visible_product
from mountain_intelligence.catalog.taxonomy import is_technical
from mountain_intelligence.sources.models import SpecificationEvidence

from .models import CompatibilityAssertion, CompatibilityRule

DISCLAIMER = (
    "Always check the physical fit with the actual boots and crampons, following both "
    "manufacturers' instructions; if in doubt ask a specialist shop or a mountain professional."
)
RELATIONS = {"boot_crampon"}


class RuleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    version: int = Field(ge=1)
    relation: str
    source_category: str
    target_category: str
    evaluator: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str
    references: list[str] = Field(default_factory=list)
    status: str = "draft"


class RuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    rules: list[RuleEntry]


@transaction.atomic
def load_rules(path: Path) -> dict[str, int]:
    data = RuleFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    stats = {"created": 0, "unchanged": 0}
    for entry in data.rules:
        if entry.evaluator not in EVALUATORS:
            raise ValueError(f"{entry.key}: unknown evaluator {entry.evaluator!r}")
        for cat in (entry.source_category, entry.target_category, *entry.parameters.get("boot_categories", [])):
            if not is_technical(cat):
                raise ValueError(f"{entry.key}: unknown or non-technical category {cat!r}")
        _validate_parameters(entry)
        existing = CompatibilityRule.objects.filter(key=entry.key, version=entry.version).first()
        payload = entry.model_dump(exclude={"key", "version"})
        if existing:
            frozen = {k: getattr(existing, k) for k in ("evaluator", "parameters", "relation")}
            if frozen != {k: payload[k] for k in frozen}:
                raise ValueError(f"{entry.key}@v{entry.version} changed; publish a new version instead")
            existing.status = entry.status
            existing.description = entry.description
            existing.references = entry.references
            existing.save()
            stats["unchanged"] += 1
        else:
            CompatibilityRule.objects.create(key=entry.key, version=entry.version, **payload)
            stats["created"] += 1
    return stats


def _validate_parameters(entry: RuleEntry) -> None:
    params = entry.parameters
    matrix = params.get("rating_matrix")
    if matrix is not None:
        if not isinstance(matrix, dict) or set(matrix) - {"B0", "B1", "B2", "B3"}:
            raise ValueError(f"{entry.key}: rating_matrix keys must be B0..B3")
        if any(set(v) - {"C1", "C2", "C3"} for v in matrix.values()):
            raise ValueError(f"{entry.key}: rating_matrix values must be lists of C1..C3")
    welts = params.get("welt_requirements")
    if welts is not None:
        if set(welts) - {"strap_on", "semi_automatic", "automatic"}:
            raise ValueError(f"{entry.key}: welt_requirements keys must be crampon binding types")
        if any(set(v) - {"toe_welt", "heel_welt"} for v in welts.values()):
            raise ValueError(f"{entry.key}: welt_requirements values must be toe_welt/heel_welt")
    large = params.get("large_boot_size_eu")
    if large is not None and not (30 <= float(large) <= 52):
        raise ValueError(f"{entry.key}: large_boot_size_eu out of range")


def _evidence(product: Product, field: str) -> list[dict[str, Any]]:
    out = []
    for e in SpecificationEvidence.objects.filter(product=product, field=field, is_current=True)[:2]:
        out.append(
            {
                "product_id": product.public_id,
                "field": field,
                "value": e.value_normalized,
                "source_url": e.source_url or None,
                "observed_at": e.observed_at.isoformat(),
                "status": e.status,
            }
        )
    if not out:
        out.append(
            {
                "product_id": product.public_id,
                "field": field,
                "value": (product.specifications or {}).get(field),
                "source_url": None,
                "observed_at": None,
                "status": "no_evidence",
            }
        )
    return out


def _binding_type_match(
    boot: Product, crampon: Product, *, boot_size_eu: float | None, include_synthetic: bool, rule: CompatibilityRule
) -> dict[str, Any]:
    b, c = boot.specifications or {}, crampon.specifications or {}
    reasons: list[str] = []
    conditions: list[str] = []
    contradictions: list[str] = []
    evidence: list[dict[str, Any]] = []

    boot_types = b.get("crampon_attachment_types")
    crampon_types = c.get("binding_types")
    evidence += _evidence(boot, "crampon_attachment_types") + _evidence(crampon, "binding_types")

    # 1) declared binding types
    type_verdict: str
    if not boot_types or not crampon_types:
        type_verdict = "unknown"
        missing = []
        if not boot_types:
            missing.append(f"{boot.public_id}: crampon compatibility not declared by the boot manufacturer")
        if not crampon_types:
            missing.append(f"{crampon.public_id}: binding type not declared")
        reasons.extend(missing)
    else:
        common = [t for t in crampon_types if t in boot_types]
        if common:
            type_verdict = "conditional"
            reasons.append(
                f"declared binding types overlap: {common} (boot declares {boot_types}, crampon offers {crampon_types})"
            )
            conditions.append(f"use the crampon in a configuration of type {common}")
        else:
            type_verdict = "incompatible"
            reasons.append(
                f"boot manufacturer declares compatibility only with {boot_types}; crampon offers {crampon_types}"
            )

    # 2) welts (physical prerequisites for heel levers / toe bails), parameterised by the rule version
    params = rule.parameters or {}
    welt_requirements: dict[str, list[str]] = params.get(
        "welt_requirements", {"strap_on": [], "semi_automatic": ["heel_welt"], "automatic": ["toe_welt", "heel_welt"]}
    )
    for t in crampon_types or []:
        needed = welt_requirements.get(t, [])
        lacking = [w for w in needed if b.get(w) is False]
        if lacking:
            for welt in lacking:
                evidence += _evidence(boot, welt)
            if boot_types and t in boot_types:
                contradictions.append(f"boot declares {t} compatibility but also declares the welt it needs is missing")
            elif crampon_types == [t]:
                type_verdict = "incompatible"
                reasons.append(f"{t} binding needs {' and '.join(needed)} that the boot declares it lacks")
        elif needed and any(b.get(w) is None for w in needed) and boot_types and t in boot_types:
            conditions.append(f"{t} binding: confirm the boot really has a usable {' and '.join(needed)}")

    # 3) B/C stiffness ratings (documented guidance: a crampon should not be stiffer than the boot)
    boot_rating, crampon_rating = b.get("boot_rating"), c.get("crampon_rating")
    matrix: dict[str, list[str]] | None = params.get("rating_matrix")
    if boot_rating and crampon_rating:
        evidence += _evidence(boot, "boot_rating") + _evidence(crampon, "crampon_rating")
        if matrix is not None and boot_rating in matrix:
            allowed = matrix[boot_rating]
            if crampon_rating in allowed:
                reasons.append(f"declared ratings {boot_rating}/{crampon_rating} are within the B/C guidance")
            else:
                type_verdict = "incompatible"
                reasons.append(
                    f"declared ratings {boot_rating}/{crampon_rating} fall outside the B/C guidance "
                    f"({boot_rating} accepts {allowed or 'no crampons'})"
                )
        else:
            reasons.append(
                f"declared ratings: boot {boot_rating}, crampon {crampon_rating} "
                "(informative; ratings do not replace the manufacturers' fit instructions)"
            )
    elif matrix is not None:
        conditions.append("B/C rating not declared for both products: check sole stiffness against the crampon type")
    large = params.get("large_boot_size_eu")
    if large is not None and boot_size_eu is not None and boot_size_eu >= large:
        conditions.append(
            f"large boot sizes (EU {large}+) flex more than their nominal rating: check stiffness, and whether "
            "an extra-long linking bar is needed"
        )

    # 4) size range
    lo, hi = c.get("boot_size_min_eu"), c.get("boot_size_max_eu")
    if boot_size_eu is not None and lo is not None and hi is not None:
        evidence += _evidence(crampon, "boot_size_min_eu")
        if not lo <= boot_size_eu <= hi:
            type_verdict = "incompatible"
            reasons.append(f"boot size EU {boot_size_eu} outside the crampon's declared range {lo}-{hi}")
        else:
            reasons.append(f"boot size EU {boot_size_eu} inside the declared range {lo}-{hi}")
    elif lo is not None and hi is not None:
        conditions.append(f"check that your boot size is within the declared range EU {lo}-{hi}")
    else:
        conditions.append("crampon size range not declared: check the adjustment range for your boot size")

    # 5) pair-level statements
    assertions = CompatibilityAssertion.objects.filter(relation="boot_crampon").filter(
        Q(product_a=boot, product_b=crampon) | Q(product_a=crampon, product_b=boot)
    )
    if not include_synthetic:
        assertions = assertions.exclude(data_status=DataStatus.SYNTHETIC_FIXTURE)
    verdicts = set()
    for a in assertions:
        verdicts.add(a.verdict)
        evidence.append(
            {
                "product_id": f"{boot.public_id}+{crampon.public_id}",
                "field": "pair_statement",
                "value": a.verdict,
                "source_url": a.source_url or None,
                "observed_at": a.observed_at.isoformat(),
                "status": f"declared_by: {a.declared_by}",
            }
        )
        if a.conditions:
            conditions.append(a.conditions)
    if len(verdicts) > 1:
        contradictions.append(f"pair-level statements disagree: {sorted(verdicts)}")

    status = type_verdict
    if verdicts == {"incompatible"}:
        status = "incompatible"
    elif verdicts == {"compatible"}:
        if type_verdict == "incompatible":
            contradictions.append("a pair-level statement says compatible but declared specifications exclude it")
        else:
            status = "compatible"
            reasons.append("documented pair-level compatibility statement")
    elif verdicts == {"conditional"} and type_verdict != "incompatible":
        status = "conditional"
    if contradictions:
        status = "unknown"
        reasons.append("contradictory evidence: manual verification required")
    if status == "conditional":
        reasons.append("compatibility of binding types only; the specific pair is not confirmed")
    return {
        "status": status,
        "reasons": reasons,
        "conditions": list(dict.fromkeys(conditions)),
        "contradictions": contradictions,
        "evidence": evidence,
    }


EVALUATORS = {"binding_type_match": _binding_type_match}


def check_compatibility(
    product_ids: list[str],
    *,
    relation: str = "boot_crampon",
    boot_size_eu: float | None = None,
    include_synthetic: bool,
) -> dict[str, Any]:
    if relation not in RELATIONS:
        raise QueryValidationError(f"unsupported relation {relation!r}; supported: {sorted(RELATIONS)}")
    if len(product_ids) != 2:
        raise QueryValidationError("check_compatibility expects exactly two product ids")
    products = [get_visible_product(pid, include_synthetic) for pid in product_ids]
    unclassified = [p.public_id for p in products if not is_technical(p.category.slug)]
    if unclassified:
        raise QueryValidationError(
            f"{unclassified} have no technical classification (uncategorized): compatibility checks need "
            "technical data (binding types, welts, sizes) and are not available for them"
        )
    rule = (
        CompatibilityRule.objects.filter(
            relation=relation, status__in=["published", "demo"] if include_synthetic else ["published"]
        )
        .order_by("-version")
        .first()
    )
    if rule is None:
        raise NotFoundError(f"no published compatibility rule for relation {relation!r}")
    boot_categories = set(rule.parameters.get("boot_categories", [rule.source_category]))
    boots = [p for p in products if p.category.slug in boot_categories]
    crampons = [p for p in products if p.category.slug == rule.target_category]
    if len(boots) != 1 or len(crampons) != 1:
        raise QueryValidationError(
            f"relation {relation!r} needs one boot ({sorted(boot_categories)}) and one {rule.target_category}; "
            f"got {[p.category.slug for p in products]}"
        )
    result = EVALUATORS[rule.evaluator](
        boots[0], crampons[0], boot_size_eu=boot_size_eu, include_synthetic=include_synthetic, rule=rule
    )
    return {
        "relation": relation,
        "boot": boots[0].public_id,
        "crampon": crampons[0].public_id,
        **result,
        "rule": {
            "key": rule.key,
            "version": rule.version,
            "status": rule.status,
            "description": rule.description,
            "references": rule.references,
        },
        "evaluated_at": timezone.now().isoformat(),
        "disclaimer": DISCLAIMER,
    }
