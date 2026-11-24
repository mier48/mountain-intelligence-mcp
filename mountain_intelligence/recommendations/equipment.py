"""Activity requirements and equipment checklists derived from reviewed activity profiles."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.conf import settings

from mountain_intelligence.catalog.queries import NotFoundError, QueryValidationError
from mountain_intelligence.catalog.services import visible_products
from mountain_intelligence.catalog.taxonomy import CATEGORIES, TECHNICAL_CATEGORIES, get_category

from . import criteria
from .engine import context_warnings, recommend, resolve_profile
from .models import ActivityProfile
from .profiles import AppliesIf, Conditions, Experience, profile_disclaimer

SCOPE_NOTE = (
    "Orientative equipment requirements only. They are not a safety assessment of any route or "
    "activity: conditions, avalanche/weather bulletins, training and experience decide whether to go."
)


def _applicability(applies_if: AppliesIf | None, conditions: Conditions, experience: Experience | None) -> str:
    if applies_if is None:
        return "applies"
    result = applies_if.evaluate(conditions, experience)
    return {True: "applies", False: "not_applicable", None: "applies_unknown_condition"}[result]


def list_activities() -> list[dict[str, Any]]:
    return [
        {
            "key": p.key,
            "name_en": p.name_en,
            "name_es": p.name_es,
            "version": p.version,
            "review_status": p.review_status,
        }
        for p in ActivityProfile.objects.filter(is_active=True).order_by("key")
    ]


def activity_requirements(
    *, activity: str, conditions: Conditions | None = None, experience: Experience | None = None
) -> dict[str, Any]:
    conditions = conditions or Conditions()
    profile, pdef = resolve_profile(activity)
    warnings = context_warnings(pdef, conditions)
    if disclaimer := profile_disclaimer(profile):
        warnings.append(disclaimer)
    needs = []
    for need in pdef.needs:
        applicability = _applicability(need.applies_if, conditions, experience)
        if applicability == "not_applicable":
            continue
        needs.append(
            {
                "category": need.category,
                "category_name_es": CATEGORIES[need.category].name_es,
                "importance": need.importance,
                "applicability": applicability,
                "rationale": need.rationale,
                "requirements": [
                    {
                        "criterion": criteria.describe(r.field, r.op, r.value),
                        "mandatory": r.mandatory,
                        "applicability": _applicability(r.applies_if, conditions, experience),
                        "rationale": r.rationale,
                    }
                    for r in need.requirements
                    if _applicability(r.applies_if, conditions, experience) != "not_applicable"
                ],
                "preferences": [
                    {
                        "criterion": criteria.describe(p.field, p.op, p.value),
                        "weight": p.weight,
                        "applicability": _applicability(p.applies_if, conditions, experience),
                        "rationale": p.rationale,
                    }
                    for p in need.preferences
                    if _applicability(p.applies_if, conditions, experience) != "not_applicable"
                ],
            }
        )
    return {
        "activity": activity,
        "profile": {
            "key": profile.key,
            "version": profile.version,
            "name_es": profile.name_es,
            "name_en": profile.name_en,
            "review_status": profile.review_status,
            "reviewed_by": profile.reviewed_by or None,
            "reviewed_at": profile.reviewed_at.isoformat() if profile.reviewed_at else None,
        },
        "description": pdef.description_es,
        "needs": needs,
        "general_notes": pdef.general_notes,
        "references": pdef.references,
        "missing_context": [k for k in pdef.context_questions if getattr(conditions, k, None) is None],
        "scope_note": SCOPE_NOTE,
        "warnings": warnings,
    }


def build_equipment_list(
    *,
    activity: str,
    conditions: Conditions | None = None,
    experience: Experience | None = None,
    owned_items: list[dict[str, str | None]] | None = None,
    budget_eur_total: float | None = None,
    budget_by_category: dict[str, float] | None = None,
    candidates_per_category: int = 3,
    include_synthetic: bool | None = None,
) -> dict[str, Any]:
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    conditions = conditions or Conditions()
    budget_by_category = budget_by_category or {}
    for cat in budget_by_category:
        if cat not in TECHNICAL_CATEGORIES:
            raise QueryValidationError(f"unknown or non-technical category in budget_by_category: {cat!r}")
    profile, pdef = resolve_profile(activity)
    requirements = activity_requirements(activity=activity, conditions=conditions, experience=experience)
    owned_by_cat: dict[str, list[dict[str, str | None]]] = {}
    for item in (owned_items or [])[:30]:
        cat = (item.get("category") or "").strip()
        if cat not in TECHNICAL_CATEGORIES:
            raise QueryValidationError(
                f"owned item has unknown or non-technical category {cat!r}; use a technical category from "
                "list_categories"
            )
        owned_by_cat.setdefault(cat, []).append(item)

    lines: list[dict[str, Any]] = []
    total = Decimal(0)
    total_known = True
    for need in requirements["needs"]:
        cat = need["category"]
        line: dict[str, Any] = {
            "category": cat,
            "category_name_es": need["category_name_es"],
            "importance": need["importance"],
            "applicability": need["applicability"],
            "rationale": need["rationale"],
            "owned": [],
            "candidates": [],
            "notes": [],
        }
        owned = owned_by_cat.get(cat, [])
        owned_ids = [str(o["product_id"]) for o in owned if o.get("product_id")]
        if owned:
            line["status"] = "owned_unverified"
            for o in owned:
                entry: dict[str, Any] = {"product_id": o.get("product_id"), "description": o.get("description")}
                pid = o.get("product_id")
                if pid:
                    check = _check_owned(activity, cat, str(pid), conditions, experience, include_synthetic)
                    entry.update(check)
                line["owned"].append(entry)
            results = [e.get("meets_mandatory_requirements") for e in line["owned"]]
            if any(r is True for r in results):
                line["status"] = "owned_meets_requirements"
            elif results and all(r is False for r in results):
                line["status"] = "owned_fails_requirements"
            if line["status"] == "owned_unverified":
                line["notes"].append(
                    "owned item could not be checked against the requirements (no catalog id or "
                    "unknown values): verify it yourself"
                )
        else:
            line["status"] = "missing"
        if line["status"] in ("missing", "owned_fails_requirements"):
            if not visible_products(include_synthetic).filter(category__slug=cat).exists():
                line["notes"].append("no catalog products available in this category yet")
                total_known = False
            else:
                rec = recommend(
                    activity=activity,
                    category=cat,
                    conditions=conditions,
                    experience=experience,
                    budget_eur=budget_by_category.get(cat),
                    owned_product_ids=owned_ids,
                    limit=candidates_per_category,
                    include_synthetic=include_synthetic,
                )
                line["candidates"] = [
                    {
                        k: c[k]
                        for k in (
                            "product_id",
                            "brand",
                            "name",
                            "price_eur",
                            "score",
                            "coverage",
                            "partial_ranking",
                            "data_status",
                        )
                    }
                    for c in rec["recommended"]
                ]
                line["requires_verification_count"] = len(rec["requires_verification"])
                top = rec["recommended"][0] if rec["recommended"] else None
                if top and top["price_eur"] is not None and need["importance"] == "essential":
                    total += Decimal(str(top["price_eur"]))
                elif need["importance"] == "essential":
                    total_known = False
                if not rec["recommended"]:
                    line["notes"].append("no candidate meets every mandatory requirement with known data")
        lines.append(line)

    estimate: dict[str, Any] = {
        "essential_top_candidates_total_eur": float(total) if total_known else None,
        "note": "sum of the top candidate per missing essential category, only when every "
        "applicable price is known; not a global budget optimization",
    }
    if budget_eur_total is not None:
        estimate["budget_eur_total"] = budget_eur_total
        estimate["within_budget"] = (float(total) <= budget_eur_total) if total_known else None
    warnings = list(requirements["warnings"])
    return {
        "activity": activity,
        "profile": requirements["profile"],
        "items": lines,
        "estimate": estimate,
        "missing_context": requirements["missing_context"],
        "general_notes": pdef.general_notes,
        "scope_note": SCOPE_NOTE,
        "warnings": warnings,
        "profile_disclaimer": profile_disclaimer(profile),
    }


def _check_owned(
    activity: str,
    category: str,
    product_id: str,
    conditions: Conditions,
    experience: Experience | None,
    include_synthetic: bool,
) -> dict[str, Any]:
    from mountain_intelligence.catalog.queries import get_visible_product

    try:
        product = get_visible_product(product_id, include_synthetic)
    except NotFoundError:
        return {"meets_mandatory_requirements": None, "check_note": "product not found in the catalog"}
    if product.category.slug != category:
        return {
            "meets_mandatory_requirements": None,
            "check_note": f"product is a {product.category.slug}, not a {category}",
        }
    _, pdef = resolve_profile(activity)
    need = next((n for n in pdef.needs if n.category == category), None)
    definition = get_category(category)
    results = []
    failed = []
    for req in need.requirements if need else []:
        applies = req.applies_if.evaluate(conditions, experience) if req.applies_if else True
        if applies is False or not req.mandatory:
            continue
        value = criteria.normalize_criterion_value(definition, req.field, req.op, req.value)
        r = criteria.evaluate(product, req.field, req.op, value)
        results.append(r)
        if r is not True:
            failed.append(
                {
                    "criterion": criteria.describe(req.field, req.op, value),
                    "result": "fail" if r is False else "unknown",
                }
            )
    meets = False if any(r is False for r in results) else (None if any(r is None for r in results) else True)
    out: dict[str, Any] = {"meets_mandatory_requirements": meets, "unmet_or_unknown": failed}
    if not results:
        out["check_note"] = "the profile defines no mandatory requirement for this category in these conditions"
    return out
