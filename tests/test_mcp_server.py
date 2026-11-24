"""MCP layer: tool registration, contracts and error mapping (in-process client)."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

from mountain_intelligence.mcp_server.tools import build_server

pytestmark = pytest.mark.django_db(transaction=True)

EXPECTED_TOOLS = {
    "list_categories",
    "list_brands",
    "search_products",
    "get_product",
    "compare_products",
    "recommend_products",
    "check_compatibility",
    "get_activity_requirements",
    "build_equipment_list",
}


def call(name: str, args: dict):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool(name, args)

    return anyio.run(run)


def test_tools_are_registered_read_only_with_schemas(reference_data):
    async def run():
        async with Client(build_server()) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(run)
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
        assert tool.output_schema and "status" in tool.output_schema["properties"]
        assert tool.description and len(tool.description) > 40
    search = next(t for t in tools if t.name == "search_products")
    assert "mountaineering_boot" in str(search.input_schema)


def test_search_and_meta(demo_catalog):
    r = call("search_products", {"query": "crampones", "limit": 2})
    assert not r.is_error
    sc = r.structured_content
    assert sc["status"] == "partial" and sc["meta"]["server"] == "mountain-intelligence"
    assert sc["meta"]["catalog_scope"] == "published_products_including_synthetic_demo_data"
    assert len(sc["data"]) == 2 and sc["pagination"]["next_cursor"]


def test_validation_errors_are_tool_errors(demo_catalog):
    r = call("search_products", {"category": "crampon", "filters": [{"field": "nope", "op": "eq", "value": 1}]})
    assert r.is_error and "validation_error" in r.content[0].text
    r = call("search_products", {"category": "kayak"})
    assert r.is_error
    r = call("compare_products", {"product_ids": ["only-one"]})
    assert r.is_error


def test_not_found_is_a_structured_status(demo_catalog):
    r = call("get_product", {"product_id": "does-not-exist"})
    assert not r.is_error and r.structured_content["status"] == "not_found"
    r = call("recommend_products", {"activity": "base_jumping", "category": "crampon"})
    assert r.structured_content["status"] == "not_found"


def test_empty_scope_reports_insufficient_data(reference_data, settings):
    settings.MI_INCLUDE_SYNTHETIC_DATA = False
    r = call("search_products", {"query": "boots"})
    assert r.structured_content["status"] == "insufficient_data" and r.structured_content["data"] == []
    r = call("list_categories", {})
    assert r.structured_content["meta"]["catalog_scope"] == "published_products"
    assert "empty" in r.structured_content["warnings"][0]


def test_list_brands(demo_catalog):
    sc = call("list_brands", {}).structured_content
    names = [b["name"] for b in sc["data"]]
    assert names == sorted(names) and "Example Outdoor" in names
    assert sc["status"] == "partial" and "SYNTHETIC" in sc["warnings"][0]
    assert sc["coverage"]["matched_products"] == sum(b["published_products"] for b in sc["data"]) > 0
    crampons = call("list_brands", {"category": "crampon"}).structured_content["data"]
    assert crampons and all("crampon" in b["categories"] for b in crampons)
    assert call("list_brands", {"category": "kayak"}).is_error


def test_list_brands_hides_synthetic_and_keeps_empty_brands(demo_catalog, settings):
    from mountain_intelligence.catalog.services import get_or_create_brand

    get_or_create_brand("Real Brand", official_domain="real-brand.example")
    settings.MI_INCLUDE_SYNTHETIC_DATA = False
    sc = call("list_brands", {}).structured_content
    assert [b["name"] for b in sc["data"]] == ["Real Brand"]
    assert sc["data"][0]["published_products"] == 0 and sc["data"][0]["official_domain"] == "real-brand.example"
    assert sc["status"] == "insufficient_data" and "empty" in sc["warnings"][0]
    sc = call("list_brands", {"category": "crampon"}).structured_content
    assert sc["data"] == [] and sc["status"] == "insufficient_data"


def test_every_tool_end_to_end(demo_catalog):
    assert call("list_categories", {"activity": "winter_mountaineering"}).structured_content["data"]["categories"]
    assert call("list_brands", {}).structured_content["data"]
    assert call("get_product", {"product_id": "example-outdoor-alpine-gtx-example"}).structured_content["data"]
    assert call(
        "compare_products",
        {"product_ids": ["example-outdoor-peak-35-example", "example-outdoor-peak-light-32-example"]},
    ).structured_content["data"]["rows"]
    rec = call(
        "recommend_products",
        {
            "activity": "winter_mountaineering",
            "category": "mountaineering_boot",
            "conditions": {"snow": True, "ice": True, "min_temperature_c": -5},
            "experience": "beginner",
            "budget_eur": 350,
        },
    ).structured_content
    assert rec["data"]["recommended"] and rec["data"]["methodology"]["version"] == "rules-v1"
    comp = call(
        "check_compatibility",
        {"product_ids": ["example-outdoor-alpine-gtx-example", "example-outdoor-glacier-strap-12"]},
    ).structured_content
    assert comp["data"]["status"] == "conditional" and comp["status"] == "partial"
    req = call("get_activity_requirements", {"activity": "winter_mountaineering"}).structured_content
    assert req["status"] == "partial" and req["data"]["missing_context"]
    eq = call(
        "build_equipment_list",
        {
            "activity": "winter_mountaineering",
            "conditions": {"snow": True, "ice": True, "min_temperature_c": -5},
            "owned_items": [{"category": "hardshell_jacket"}],
        },
    ).structured_content
    assert {i["category"] for i in eq["data"]["items"]} >= {"crampon", "mountaineering_boot"}


def test_uncategorized_is_listed_but_not_accepted_where_technical_data_is_required(demo_catalog):
    r = call("list_categories", {})
    cats = {c["slug"]: c for c in r.structured_content["data"]["categories"]}
    assert cats["uncategorized"]["technical"] is False and cats["uncategorized"]["fields"] == []
    assert all(c["technical"] for slug, c in cats.items() if slug != "uncategorized")
    rec = call("recommend_products", {"activity": "winter_mountaineering", "category": "uncategorized"})
    assert rec.is_error  # rejected by the input schema (technical categories only)
    search = call("search_products", {"category": "uncategorized", "classification": "uncategorized"})
    assert not search.is_error and search.structured_content["status"] == "insufficient_data"
