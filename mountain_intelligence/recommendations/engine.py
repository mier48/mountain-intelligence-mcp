"""Deterministic, explainable recommendation engine.

1. Resolve the active activity profile and the applicable requirements for the context.
2. Apply mandatory requirements, user-required features and budget as exclusion filters with
   tri-state logic: a candidate with an *unknown* mandatory value is never treated as passing;
   it goes to `requires_verification`. An unknown price is never "within budget".
3. Rank eligible candidates with versioned weights over normalized [0, 1] components. A missing
   value scores 0 for that component (no reward for missing data) and the result is flagged as
   partial. Data coverage is reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.catalog.queries import (
    NotFoundError,
    QueryValidationError,
    SpecFilter,
    product_summary,
)
from mountain_intelligence.catalog.services import brand_slug, coverage, spec_value, visible_products
from mountain_intelligence.catalog.taxonomy import (
    CATEGORIES,
    TECHNICAL_CATEGORIES,
    CategoryDefinition,
    get_category,
)
from mountain_intelligence.offers.services import comparable_price_eur

from . import criteria
from .models import ActivityProfile
from .profiles import (
    CategoryNeed,
    Conditions,
    Experience,
    ProfileDefinition,
    active_profile,
    profile_disclaimer,
)

SCORING_VERSION = "rules-v1"
MAX_CANDIDATES = 500
WEIGHT_FIELD = {"footwear": "weight_per_boot_g", "crampon": "weight_per_pair_g"}


def weight_field_for(definition: CategoryDefinition) -> str | None:
    if definition.slug == "crampon":
        return "weight_per_pair_g"
    if definition.family == "footwear":
        return "weight_per_boot_g"
    return "weight_g" if "weight_g" in definition.field_map else None


@dataclass
class Check:
    criterion: str
    result: bool | None
    rationale: str
    source: str  # profile | user | budget

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "result": {True: "pass", False: "fail", None: "unknown"}[self.result],
            "rationale": self.rationale,
            "source": self.source,
        }


@dataclass
class Candidate:
    product: Product
    checks: list[Check] = field(default_factory=list)
    price: float | None = None
    coverage: float = 0.0
    score: float = 0.0
    breakdown: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def resolve_profile(activity: str) -> tuple[ActivityProfile, ProfileDefinition]:
    resolved = active_profile(activity)
    if resolved is None:
        available = list(ActivityProfile.objects.filter(is_active=True).values_list("key", flat=True))
        raise NotFoundError(f"unknown activity {activity!r}; available activity profiles: {available}")
    return resolved


def find_need(definition: ProfileDefinition, category: str) -> CategoryNeed | None:
    return next((n for n in definition.needs if n.category == category), None)


def context_warnings(definition: ProfileDefinition, conditions: Conditions) -> list[str]:
    missing = [k for k in definition.context_questions if getattr(conditions, k, None) is None]
    if not missing:
        return []
    return [
        f"missing context {missing}: requirements that depend on it were applied conservatively "
        "(treated as applicable). Provide these values for a more precise answer."
    ]


def recommend(
    *,
    activity: str,
    category: str,
    conditions: Conditions | None = None,
    experience: Experience | None = None,
    budget_eur: float | None = None,
    required_features: list[SpecFilter] | None = None,
    preferred_brands: list[str] | None = None,
    owned_product_ids: list[str] | None = None,
    limit: int = 5,
    include_synthetic: bool | None = None,
) -> dict[str, Any]:
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    conditions = conditions or Conditions()
    limit = max(1, min(limit, 10))
    if category not in CATEGORIES:
        raise QueryValidationError(f"unknown category {category!r}")
    definition = get_category(category)
    if not definition.technical:
        raise QueryValidationError(
            f"{category!r} products have no technical classification: recommendations need a technical "
            f"category ({sorted(TECHNICAL_CATEGORIES)})"
        )
    profile, pdef = resolve_profile(activity)
    warnings = context_warnings(pdef, conditions)
    if experience is None:
        warnings.append("experience level not provided")
    if disclaimer := profile_disclaimer(profile):
        warnings.append(disclaimer)
    need = find_need(pdef, category)
    if need is None:
        warnings.append(
            f"category {category!r} is not part of the {activity!r} profile: only your own required "
            "features and budget were applied"
        )
    elif need.applies_if and need.applies_if.evaluate(conditions, experience) is False:
        warnings.append(f"the {activity!r} profile does not list {category!r} as needed for these conditions")

    # Build the exclusion criteria
    mandatory: list[tuple[str, str, Any, str, str]] = []  # field, op, value, rationale, origin
    preferences: list[tuple[str, str, Any, float, str]] = []
    if need is not None:
        for req in need.requirements:
            applies = req.applies_if.evaluate(conditions, experience) if req.applies_if else True
            if applies is False or not req.mandatory:
                continue
            value = criteria.normalize_criterion_value(definition, req.field, req.op, req.value)
            rationale = req.rationale + (
                " (applied because a condition it depends on is unknown)" if applies is None else ""
            )
            mandatory.append((req.field, req.op, value, rationale, "profile"))
        for pref in need.preferences:
            applies = pref.applies_if.evaluate(conditions, experience) if pref.applies_if else True
            if applies is False:
                continue
            value = criteria.normalize_criterion_value(definition, pref.field, pref.op, pref.value)
            preferences.append((pref.field, pref.op, value, pref.weight, pref.rationale))
    for feature in (required_features or [])[:12]:
        value = criteria.normalize_criterion_value(definition, feature.field, feature.op, feature.value)
        mandatory.append((feature.field, feature.op, value, "required by the user", "user"))

    owned = {pid.strip().lower() for pid in owned_product_ids or []}
    qs = visible_products(include_synthetic).filter(category__slug=category).exclude(public_id__in=owned)
    products = list(qs.prefetch_related("offers")[:MAX_CANDIDATES])
    if len(products) == MAX_CANDIDATES:
        warnings.append(f"candidate set truncated to {MAX_CANDIDATES} products")

    eligible: list[Candidate] = []
    verify: list[Candidate] = []
    excluded: list[Candidate] = []
    for product in products:
        cand = Candidate(product=product, coverage=coverage(product, definition))
        price = comparable_price_eur(product, include_synthetic=include_synthetic)
        cand.price = float(price.amount) if price else None
        for fld, op, value, rationale, origin in mandatory:
            cand.checks.append(
                Check(criteria.describe(fld, op, value), criteria.evaluate(product, fld, op, value), rationale, origin)
            )
        if budget_eur is not None:
            result = None if cand.price is None else cand.price <= budget_eur
            cand.checks.append(
                Check(
                    f"price_eur <= {budget_eur}",
                    result,
                    "no recent comparable EUR price" if cand.price is None else f"lowest recent EUR price {cand.price}",
                    "budget",
                )
            )
        results = [c.result for c in cand.checks]
        if any(r is False for r in results):
            excluded.append(cand)
        elif any(r is None for r in results):
            verify.append(cand)
        elif cand.coverage == 0:
            # Without a single known key specification nothing technical can be asserted.
            cand.checks.append(
                Check("technical data", None, "no key technical specification is known for this product", "profile")
            )
            verify.append(cand)
        elif need is not None and cand.coverage < need.min_coverage:
            cand.checks.append(
                Check(
                    "data coverage",
                    None,
                    f"coverage {cand.coverage} below the profile minimum {need.min_coverage}",
                    "profile",
                )
            )
            verify.append(cand)
        else:
            eligible.append(cand)

    weights = need.scoring_weights if need else None
    weights_used = _score(eligible + verify, definition, preferences, preferred_brands or [], weights)
    eligible.sort(key=lambda c: (-c.score, -c.coverage, c.product.public_id))
    verify.sort(key=lambda c: (-c.score, -c.coverage, c.product.public_id))

    def render(c: Candidate, *, with_score: bool = True) -> dict[str, Any]:
        out = product_summary(c.product, include_synthetic=include_synthetic)
        out["checks"] = [ch.as_dict() for ch in c.checks]
        if with_score:
            out["score"] = round(c.score, 3)
            out["score_breakdown"] = c.breakdown
            out["partial_ranking"] = bool(c.missing)
            out["missing_criteria"] = c.missing
        return out

    if not products:
        warnings.append(f"no published products in category {category!r} for this catalog scope")
    return {
        "activity": activity,
        "category": category,
        "profile": {"key": profile.key, "version": profile.version, "review_status": profile.review_status},
        "recommended": [render(c) for c in eligible[:limit]],
        "requires_verification": [render(c) for c in verify[:limit]],
        "excluded_count": len(excluded),
        "excluded_examples": [
            {"product_id": c.product.public_id, "failed": [ch.as_dict() for ch in c.checks if ch.result is False]}
            for c in excluded[:10]
        ],
        "candidates_evaluated": len(products),
        "methodology": {
            "version": SCORING_VERSION,
            "weights": weights_used,
            "notes": [
                "mandatory requirements and budget are exclusion filters, not score components",
                "unknown mandatory values send candidates to requires_verification",
                "missing optional data scores 0 for that component and marks the ranking as partial",
                "weight and price are normalized among the evaluated candidates (relative, not absolute)",
                "scores are only comparable within this response",
            ],
        },
        "warnings": warnings,
    }


def _score(
    cands: list[Candidate],
    definition: CategoryDefinition,
    preferences: list[tuple[str, str, Any, float, str]],
    preferred_brands: list[str],
    weights_cfg,
) -> dict[str, float]:
    base = (
        weights_cfg.model_dump()
        if weights_cfg is not None
        else {"technical_fit": 0.45, "weight": 0.20, "price": 0.20, "preferences": 0.15}
    )
    wfield = weight_field_for(definition)
    weight_values = {id(c): spec_value(c.product, wfield) for c in cands} if wfield else {}
    known_w = [v for v in weight_values.values() if v is not None]
    known_p = [c.price for c in cands if c.price is not None]
    applicable = {
        "technical_fit": bool(preferences),
        "weight": len(known_w) >= 1 and wfield is not None,
        "price": len(known_p) >= 1,
        "preferences": bool(preferred_brands),
    }
    total = sum(base[k] for k, ok in applicable.items() if ok)
    weights = {k: round(base[k] / total, 4) if ok and total else 0.0 for k, ok in applicable.items()}
    brand_slugs = {brand_slug(b) for b in preferred_brands}

    def relative(value: float | None, values: list[float]) -> float | None:
        if value is None:
            return None
        lo, hi = min(values), max(values)
        return 1.0 if hi == lo else (hi - value) / (hi - lo)

    for c in cands:
        parts: dict[str, Any] = {}
        missing: list[str] = []
        if applicable["technical_fit"]:
            total_w = sum(p[3] for p in preferences)
            got, details = 0.0, []
            for fld, op, value, w, rationale in preferences:
                r = criteria.evaluate(c.product, fld, op, value)
                if r is None:
                    missing.append(fld)
                got += w if r else 0.0
                details.append(
                    {
                        "criterion": criteria.describe(fld, op, value),
                        "weight": w,
                        "result": {True: "pass", False: "fail", None: "unknown"}[r],
                        "rationale": rationale,
                    }
                )
            parts["technical_fit"] = {
                "score": round(got / total_w, 3),
                "weight": weights["technical_fit"],
                "details": details,
            }
        if applicable["weight"]:
            s = relative(weight_values.get(id(c)), known_w)
            if s is None:
                missing.append(wfield or "weight")
            parts["weight"] = {
                "score": round(s or 0.0, 3),
                "weight": weights["weight"],
                "field": wfield,
                "value": weight_values.get(id(c)),
                "note": "lighter scores higher (relative)",
            }
        if applicable["price"]:
            s = relative(c.price, known_p)
            if s is None:
                missing.append("price_eur")
            parts["price"] = {
                "score": round(s or 0.0, 3),
                "weight": weights["price"],
                "value": c.price,
                "note": "cheaper scores higher (relative)",
            }
        if applicable["preferences"]:
            hit = c.product.brand.slug in brand_slugs
            parts["preferences"] = {
                "score": 1.0 if hit else 0.0,
                "weight": weights["preferences"],
                "note": "preferred brand" if hit else "not a preferred brand",
            }
        c.score = sum(p["score"] * p["weight"] for p in parts.values())
        c.breakdown = parts
        c.missing = sorted(set(missing))
    return weights
