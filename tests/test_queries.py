"""Search, filters, pagination, synthetic isolation, product detail and comparison."""

from __future__ import annotations

import pytest

from mountain_intelligence.catalog.queries import (
    NotFoundError,
    QueryValidationError,
    SpecFilter,
    compare_products,
    product_detail,
    search_products,
)

pytestmark = pytest.mark.django_db

ALPINE = "example-outdoor-alpine-gtx-example"
SUMMIT = "example-outdoor-summit-pro-example"
TREK = "demo-peaks-trek-winter-lite"


def ids(result):
    return [i["product_id"] for i in result["items"]]


def test_synthetic_data_is_excluded_by_default(demo_catalog, settings):
    settings.MI_INCLUDE_SYNTHETIC_DATA = False
    assert search_products(query="boot")["total"] == 0
    with pytest.raises(NotFoundError):
        product_detail(ALPINE)
    settings.MI_INCLUDE_SYNTHETIC_DATA = True
    result = search_products(query="boot")
    assert result["total"] > 0
    assert "SYNTHETIC" in result["warnings"][0]


def test_bilingual_text_search(demo_catalog):
    es = ids(search_products(query="botas de alpinismo", limit=25))
    en = ids(search_products(query="mountaineering boots", limit=25))
    assert ALPINE in es and set(es) == set(en)
    crampons = ids(search_products(query="crampones semiautomáticos"))
    assert crampons and all("crampon" in c or "lever" in c or "alu" in c for c in crampons)


def test_identifier_search_ranks_exact_match_first(demo_catalog):
    assert ids(search_products(query="DEMO-00002"))[0] == ALPINE
    assert search_products(query="4")["total"] == 0  # single characters are ignored, no crash


def test_typed_filters(demo_catalog):
    light = search_products(
        category="mountaineering_boot", limit=25, filters=[SpecFilter(field="weight_per_boot_g", op="lte", value=760)]
    )
    assert set(ids(light)) == {ALPINE, "synthetic-alpine-co-couloir-light-2", "synthetic-alpine-co-ridge-mid"}
    # TREK has weight 680 but unknown basis: unknown never matches a filter
    assert TREK not in ids(light)
    semi = search_products(
        category="crampon",
        filters=[
            SpecFilter(field="binding_types", op="contains", value=["semi_automatic"]),
            SpecFilter(field="anti_balling_plates", op="eq", value=True),
        ],
    )
    assert ids(semi) == ["demo-peaks-ice-lever-12"]
    shells = search_products(
        category="hardshell_jacket",
        filters=[
            SpecFilter(field="ventilation", op="contains", value="pit zips"),
            SpecFilter(field="weight_g", op="lte", value="0.45 kg"),
        ],
    )
    assert ids(shells) == ["example-outdoor-storm-shell-3l-example"]


@pytest.mark.parametrize(
    ("flt", "message"),
    [
        (SpecFilter(field="colour", op="eq", value="red"), "not filterable"),
        (SpecFilter(field="weight_g", op="contains", value=1), "not supported"),
        (SpecFilter(field="rigidity", op="eq", value="bendy"), "invalid value"),
        (SpecFilter(field="membrane", op="eq", value="x"), "not filterable"),
    ],
)
def test_invalid_filters_are_rejected(demo_catalog, flt, message):
    with pytest.raises(QueryValidationError, match=message):
        search_products(category="mountaineering_boot", filters=[flt])


def test_filters_require_category(demo_catalog):
    with pytest.raises(QueryValidationError, match="require a category"):
        search_products(filters=[SpecFilter(field="weight_g", op="lte", value=1)])


def test_stable_pagination(demo_catalog):
    first = search_products(category="backpack", limit=2)
    second = search_products(category="backpack", limit=2, cursor=first["next_cursor"])
    third = search_products(category="backpack", limit=2, cursor=second["next_cursor"])
    all_ids = ids(first) + ids(second) + ids(third)
    assert len(all_ids) == len(set(all_ids)) == first["total"] == 5
    assert third["next_cursor"] is None
    with pytest.raises(QueryValidationError, match="does not belong"):
        search_products(category="crampon", limit=2, cursor=first["next_cursor"])
    with pytest.raises(QueryValidationError, match="invalid cursor"):
        search_products(category="backpack", cursor="garbage!!")


def test_product_detail_with_evidence_and_unknowns(demo_catalog):
    detail = product_detail(ALPINE)
    fields = {row["field"]: row for row in detail["specifications"]}
    assert fields["weight_g"]["value"] == 750 and fields["weight_g"]["unit"] == "g"
    assert fields["weight_g"]["evidence"][0]["status"] == "synthetic_fixture"
    assert fields["weight_g"]["evidence"][0]["source_url"].startswith("https://example.com/synthetic/")
    assert fields["insulated"]["value"] is False  # declared false
    assert fields["integrated_gaiter"]["known"] is False  # unknown stays null
    assert fields["weight_per_boot_g"]["derived"] is True
    assert len(detail["variants"]) == 4 and detail["offers"][0]["price"] == 289.0
    variant = product_detail(ALPINE, variant=detail["variants"][0]["sku"])
    assert variant["selected_variant"]
    with pytest.raises(NotFoundError):
        product_detail(ALPINE, variant="nope")


def test_comparison_flags_weight_basis_and_unknowns(demo_catalog):
    result = compare_products([ALPINE, SUMMIT, TREK])
    rows = {r["field"]: r for r in result["rows"]}
    assert rows["weight_per_boot_g"]["values"] == {ALPINE: 750, SUMMIT: 950, TREK: None}
    assert rows["weight_per_boot_g"]["unknown_for"] == [TREK]
    assert rows["weight_per_boot_g"]["range"]["min_product"] == ALPINE
    assert any("weight basis" in note for note in result["limitations"])
    assert any("reference sizes" in note for note in result["limitations"])


def test_comparison_refuses_ret_vs_mvtr(demo_catalog):
    result = compare_products(["example-outdoor-storm-shell-3l-example", "demo-peaks-trail-shell-25l"])
    assert any("RET and MVTR" in n for n in result["limitations"])
    assert any("different protocols" in n for n in result["limitations"])


def test_comparison_validation(demo_catalog):
    with pytest.raises(QueryValidationError):
        compare_products([ALPINE])
    with pytest.raises(QueryValidationError, match="unrelated categories"):
        compare_products([ALPINE, "example-outdoor-peak-35-example"])
    with pytest.raises(NotFoundError):
        compare_products([ALPINE, "missing-product"])
