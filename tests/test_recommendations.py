"""Recommendation engine, compatibility states and equipment lists."""

from __future__ import annotations

import pytest

from mountain_intelligence.catalog.queries import NotFoundError, QueryValidationError, SpecFilter
from mountain_intelligence.recommendations.compatibility import check_compatibility
from mountain_intelligence.recommendations.engine import recommend
from mountain_intelligence.recommendations.equipment import activity_requirements, build_equipment_list
from mountain_intelligence.recommendations.profiles import Conditions

pytestmark = pytest.mark.django_db
WINTER = Conditions(snow=True, ice=True, min_temperature_c=-5)


def pids(items):
    return [i["product_id"] for i in items]


def test_recommendation_excludes_and_separates_unknowns(demo_catalog):
    r = recommend(
        activity="winter_mountaineering",
        category="mountaineering_boot",
        conditions=WINTER,
        experience="beginner",
        budget_eur=350,
    )
    recommended, verify = pids(r["recommended"]), pids(r["requires_verification"])
    # Summit Pro costs 449 > 350: excluded
    assert "example-outdoor-summit-pro-example" not in recommended + verify
    assert r["excluded_count"] == 1
    # Couloir Light gen 2 has no price: never "within budget" -> verification bucket
    assert "synthetic-alpine-co-couloir-light-2" in verify
    # Ridge Mid has no declared crampon compatibility (mandatory on snow/ice) -> verification
    assert "synthetic-alpine-co-ridge-mid" in verify
    assert set(recommended) == {
        "example-outdoor-alpine-gtx-example",
        "synthetic-alpine-co-couloir-light-1",
        "demo-peaks-trek-winter-lite",
    }
    for item in r["recommended"]:
        assert all(c["result"] == "pass" for c in item["checks"])
    scores = [i["score"] for i in r["recommended"]]
    assert scores == sorted(scores, reverse=True)
    trek = next(i for i in r["recommended"] if i["product_id"] == "demo-peaks-trek-winter-lite")
    assert trek["partial_ranking"] and "weight_per_boot_g" in trek["missing_criteria"]
    assert trek["score_breakdown"]["weight"]["score"] == 0  # missing data is not rewarded
    assert any("NOT been reviewed" in w for w in r["warnings"])


def test_user_required_features_are_mandatory(demo_catalog):
    r = recommend(
        activity="winter_mountaineering",
        category="mountaineering_boot",
        conditions=WINTER,
        required_features=[SpecFilter(field="crampon_attachment_types", op="contains", value="automatic")],
    )
    assert pids(r["recommended"]) == ["example-outdoor-summit-pro-example"]


def test_unknown_conditions_apply_requirements_conservatively(demo_catalog):
    r = recommend(activity="winter_mountaineering", category="mountaineering_boot")
    assert any("missing context" in w for w in r["warnings"])
    assert "synthetic-alpine-co-ridge-mid" in pids(r["requires_verification"])
    summer = recommend(
        activity="winter_mountaineering", category="mountaineering_boot", conditions=Conditions(snow=False, ice=False)
    )
    assert "synthetic-alpine-co-ridge-mid" in pids(summer["recommended"])


def test_preferred_brands_and_owned_products(demo_catalog):
    r = recommend(
        activity="winter_mountaineering",
        category="backpack",
        preferred_brands=["Demo Peaks"],
        owned_product_ids=["example-outdoor-peak-35-example"],
    )
    assert "example-outdoor-peak-35-example" not in pids(r["recommended"]) + pids(r["requires_verification"])
    assert r["methodology"]["weights"]["preferences"] > 0


def test_recommendation_errors(demo_catalog):
    with pytest.raises(NotFoundError, match="available activity profiles"):
        recommend(activity="base_jumping", category="crampon")
    with pytest.raises(QueryValidationError):
        recommend(
            activity="winter_mountaineering",
            category="crampon",
            required_features=[SpecFilter(field="colour", op="eq", value="red")],
        )


@pytest.mark.parametrize(
    ("boot", "crampon", "size", "status"),
    [
        ("example-outdoor-alpine-gtx-example", "demo-peaks-ice-lever-12", None, "compatible"),
        ("example-outdoor-alpine-gtx-example", "example-outdoor-glacier-strap-12", None, "conditional"),
        ("example-outdoor-alpine-gtx-example", "demo-peaks-vertical-auto-14", None, "incompatible"),
        ("demo-peaks-trek-winter-lite", "demo-peaks-ice-lever-12", None, "unknown"),  # contradictory evidence
        ("synthetic-alpine-co-ridge-mid", "example-outdoor-glacier-strap-12", None, "unknown"),  # no declaration
        ("example-outdoor-alpine-gtx-example", "example-outdoor-mystery-crampon", None, "unknown"),
        ("example-outdoor-summit-pro-example", "demo-peaks-vertical-auto-14", 43, "conditional"),
        ("example-outdoor-summit-pro-example", "demo-peaks-vertical-auto-14", 48, "incompatible"),  # size range
        # binding types overlap (strap_on) but a B1 boot must not take a C2 crampon (B/C guidance, rule v2)
        ("demo-peaks-trek-winter-lite", "synthetic-alpine-co-light-alu-10", None, "incompatible"),
    ],
)
def test_compatibility_states(demo_catalog, boot, crampon, size, status):
    r = check_compatibility([crampon, boot], boot_size_eu=size, include_synthetic=True)
    assert r["status"] == status, r["reasons"]
    assert r["rule"]["key"] == "boot-crampon-binding-type" and r["rule"]["version"] == 2
    assert r["disclaimer"]
    if status == "conditional":
        assert any("not confirmed" in reason for reason in r["reasons"])


def test_compatibility_ignores_synthetic_assertions_outside_demo(demo_catalog):
    from mountain_intelligence.catalog.models import Product
    from mountain_intelligence.recommendations.compatibility import EVALUATORS
    from mountain_intelligence.recommendations.models import CompatibilityRule

    boot = Product.objects.get(public_id="example-outdoor-alpine-gtx-example")
    crampon = Product.objects.get(public_id="demo-peaks-ice-lever-12")
    rule = CompatibilityRule.objects.get(status="published")
    r = EVALUATORS[rule.evaluator](boot, crampon, boot_size_eu=None, include_synthetic=False, rule=rule)
    assert r["status"] == "conditional"


def test_compatibility_validation(demo_catalog):
    with pytest.raises(QueryValidationError):
        check_compatibility(
            ["example-outdoor-alpine-gtx-example", "example-outdoor-summit-pro-example"], include_synthetic=True
        )
    with pytest.raises(QueryValidationError):
        check_compatibility(["a", "b"], relation="pack_axe", include_synthetic=True)


def test_activity_requirements(demo_catalog):
    r = activity_requirements(
        activity="winter_mountaineering", conditions=Conditions(snow=False, ice=False, min_temperature_c=8)
    )
    cats = [n["category"] for n in r["needs"]]
    assert "crampon" not in cats and "insulated_jacket" not in cats and "mountaineering_boot" in cats
    unknown = activity_requirements(activity="winter_mountaineering")
    crampon = next(n for n in unknown["needs"] if n["category"] == "crampon")
    assert crampon["applicability"] == "applies_unknown_condition"
    assert unknown["missing_context"] == ["snow", "ice", "min_temperature_c"]


def test_equipment_list(demo_catalog):
    r = build_equipment_list(
        activity="winter_mountaineering",
        conditions=Conditions(snow=True, ice=True, min_temperature_c=-8),
        owned_items=[
            {"category": "hardshell_jacket", "description": "old shell"},
            {"category": "mountaineering_boot", "product_id": "synthetic-alpine-co-ridge-mid"},
        ],
        budget_by_category={"crampon": 130},
    )
    items = {i["category"]: i for i in r["items"]}
    assert items["hardshell_jacket"]["status"] == "owned_unverified"
    assert items["mountaineering_boot"]["status"] == "owned_unverified"  # unknown crampon declaration
    assert items["mountaineering_boot"]["candidates"] == []
    assert [c["product_id"] for c in items["crampon"]["candidates"]] == ["example-outdoor-glacier-strap-12"]
    assert items["crampon"]["status"] == "missing"
    assert r["estimate"]["essential_top_candidates_total_eur"] is not None


def test_equipment_list_validation(demo_catalog):
    with pytest.raises(QueryValidationError):
        build_equipment_list(activity="winter_mountaineering", owned_items=[{"category": "kayak"}])


def test_rating_matrix_and_large_size_notes(demo_catalog):
    r = check_compatibility(["demo-peaks-trek-winter-lite", "synthetic-alpine-co-light-alu-10"], include_synthetic=True)
    assert any("outside the B/C guidance" in reason for reason in r["reasons"])
    r = check_compatibility(
        ["example-outdoor-summit-pro-example", "example-outdoor-glacier-strap-12"],
        boot_size_eu=47,
        include_synthetic=True,
    )
    assert r["status"] == "conditional"
    assert any("large boot sizes" in c for c in r["conditions"])
    assert any("within the B/C guidance" in reason for reason in r["reasons"])


def test_standards_are_matched_tolerantly(demo_catalog):
    from mountain_intelligence.catalog.models import Product
    from mountain_intelligence.recommendations.criteria import canonical_token, evaluate

    assert canonical_token("EN 12492:2012") == canonical_token("en-12492") == "en12492"
    helmet = Product.objects.get(public_id="demo-peaks-feather-helmet")
    assert evaluate(helmet, "declared_standards", "contains", ["EN 12492:2025"]) is True
    assert evaluate(helmet, "declared_standards", "contains", ["EN 13089"]) is False


def test_new_and_updated_profiles(demo_catalog):
    r = recommend(activity="summer_alpine_mountaineering", category="climbing_helmet")
    top = r["recommended"][0]
    assert top["score_breakdown"]["technical_fit"]["details"][0]["result"] == "pass"
    winter = activity_requirements(
        activity="winter_mountaineering", conditions=Conditions(snow=True, ice=True, min_temperature_c=-5)
    )
    boots = next(n for n in winter["needs"] if n["category"] == "mountaineering_boot")
    assert any("boot_rating" in p["criterion"] for p in boots["preferences"])
    assert winter["profile"]["version"] == 2
