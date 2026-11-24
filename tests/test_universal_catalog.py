"""Universal catalog: full-catalog discovery, Shopify adapter, `uncategorized` fallback, batches,
completeness, idempotency, progressive classification and exclusion from technical features."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from mountain_intelligence.catalog.models import Brand, Category, Product, ProductVariant
from mountain_intelligence.catalog.queries import (
    QueryValidationError,
    compare_products,
    product_detail,
    search_products,
)
from mountain_intelligence.catalog.taxonomy import CATEGORIES, TECHNICAL_CATEGORIES, UNCATEGORIZED_SLUG
from mountain_intelligence.ingestion.adapters.base import AdapterContext
from mountain_intelligence.ingestion.adapters.classification import classify
from mountain_intelligence.ingestion.adapters.shopify import description_attributes, extract_from_storefront_json
from mountain_intelligence.ingestion.discovery import canonicalize_url, discover_from_sitemaps
from mountain_intelligence.ingestion.http import SafeFetcher
from mountain_intelligence.ingestion.models import IngestionRun, ReviewItem
from mountain_intelligence.ingestion.pipeline import run_source_sync
from mountain_intelligence.ingestion.tasks import due_sources
from mountain_intelligence.offers.models import Offer
from mountain_intelligence.recommendations.compatibility import check_compatibility
from mountain_intelligence.recommendations.engine import recommend
from mountain_intelligence.sources.models import Source, SourceRecord, SpecificationEvidence
from mountain_intelligence.sources.registry import Registry, SourceEntry, load_registry

FIXTURES = Path(settings.FIXTURES_DIR) / "shopify"
ORIGIN = "https://shop.test"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def storefront(name: str) -> str:
    return (FIXTURES / f"{name}.js").read_text()


# ---------------------------------------------------------------------------------- taxonomy


def test_uncategorized_is_a_non_technical_fallback():
    definition = CATEGORIES[UNCATEGORIZED_SLUG]
    assert not definition.technical and definition.fields == ()
    assert UNCATEGORIZED_SLUG not in TECHNICAL_CATEGORIES
    assert len(TECHNICAL_CATEGORIES) == 10  # the existing strict schemas are untouched
    assert definition.search_terms() == []


@pytest.mark.django_db
def test_uncategorized_category_row_exists_after_migrate():
    row = Category.objects.get(slug=UNCATEGORIZED_SLUG)
    assert row.technical is False and Category.objects.filter(technical=True).count() == 10


# ---------------------------------------------------------------------------------- canonical URLs


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Shop.test/products/x", "https://shop.test/products/x"),
        ("https://shop.test/en/products/x", "https://shop.test/products/x"),
        ("https://shop.test/es-es/products/x/", "https://shop.test/products/x"),
        ("https://shop.test/collections/mochilas/products/x?variant=1#top", "https://shop.test/products/x"),
        ("https://shop.test/en/collections/c/products/x", "https://shop.test/products/x"),
        ("https://shop.test/es/es/shop/mens/jacket", "https://shop.test/es/es/shop/mens/jacket"),
    ],
)
def test_canonicalize_url_collapses_locales_paths_and_parameters(raw, expected):
    assert canonicalize_url(raw) == expected


def test_canonical_host_option():
    assert canonicalize_url("https://www.shop.test/products/x", {"canonical_host": "shop.test"}) == (
        "https://shop.test/products/x"
    )


# ---------------------------------------------------------------------------------- mock storefront


def sitemap_index(*children: str) -> bytes:
    body = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0"?><sitemapindex xmlns="x">{body}</sitemapindex>'.encode()


def urlset(*urls: str) -> bytes:
    body = "".join(f"<url><loc>{u}</loc><lastmod>2026-11-20</lastmod></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="x">{body}</urlset>'.encode()


def xml(content: bytes) -> httpx2.Response:
    return httpx2.Response(200, content=content, headers={"content-type": "application/xml"})


def product_js(name: str, etag: str = "") -> object:
    body = storefront(name).encode()

    def respond(request: httpx2.Request) -> httpx2.Response:
        if etag and request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        headers = {"content-type": "application/json"}
        if etag:
            headers["etag"] = etag
        return httpx2.Response(200, content=body, headers=headers)

    return respond


def store_pages(handles: tuple[str, ...] = ("ridge-w-fleece", "summit-30-mochila", "domo-2p")) -> dict:
    products = [f"{ORIGIN}/products/{h}" for h in handles]
    pages: dict = {
        f"{ORIGIN}/robots.txt": httpx2.Response(200, text="User-agent: *\nAllow: /\nDisallow: /cart\n"),
        f"{ORIGIN}/sitemap.xml": xml(
            sitemap_index(
                f"{ORIGIN}/sitemap_products_1.xml?from=1&amp;to=9",
                f"{ORIGIN}/en/sitemap_products_1.xml?from=1&amp;to=9",
                f"{ORIGIN}/sitemap_pages_1.xml",
                f"{ORIGIN}/sitemap_collections_1.xml",
            )
        ),
        # /en/ copies, a collection-scoped path and a parameterized URL all collapse into one item each
        f"{ORIGIN}/sitemap_products_1.xml?from=1&to=9": xml(urlset(ORIGIN + "/", *products)),
        f"{ORIGIN}/en/sitemap_products_1.xml?from=1&to=9": xml(
            urlset(*(p.replace("/products/", "/en/products/") for p in products), f"{products[0]}?variant=91001")
        ),
        f"{ORIGIN}/sitemap_collections_1.xml": xml(urlset(f"{ORIGIN}/collections/mochilas")),
        f"{ORIGIN}/collections/mochilas/products.json?limit=250&page=1": httpx2.Response(
            200, json={"products": [{"handle": "summit-30-mochila"}]}
        ),
    }
    for handle in handles:
        pages[f"{ORIGIN}/products/{handle}.js"] = product_js(handle, etag=f'"{handle}-v1"')
    return pages


def mock_site(pages: dict, log: list[str]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        log.append(str(request.url))
        response = pages.get(str(request.url))
        if callable(response):
            return response(request)
        return response or httpx2.Response(404)

    return httpx2.MockTransport(handler)


def shop_source(*, categories: list[str] | None = None, **config) -> Source:
    brand = Brand.objects.get_or_create(slug="synthetic-outdoor", defaults={"name": "Synthetic Outdoor"})[0]
    source, _ = Source.objects.update_or_create(
        slug="shop-test",
        defaults={
            "name": "Synthetic Outdoor official store",
            "brand": brand,
            "source_type": Source.SourceType.MANUFACTURER_WEBSITE,
            "adapter": "shopify_product",
            "base_url": ORIGIN + "/",
            "allowed_domains": ["shop.test"],
            "access_status": Source.AccessStatus.AUTHORIZED,
            "enabled": True,
            "auto_publish": True,
            "sync_frequency_hours": 168,
            "categories": categories or [],
            "config": {
                "capture_offers": True,
                "currency": "EUR",
                "description_attributes": True,
                "collections": {"enabled": True},
                "category_rules": [{"pattern": r"\bmochilas?\b", "category": "backpack"}],
                **config,
            },
        },
    )
    return source


def sync(source: Source, pages: dict, log: list[str] | None = None, **kwargs) -> IngestionRun:
    fetcher = SafeFetcher(
        source.allowed_domains, transport=mock_site(pages, log if log is not None else []), min_delay=0, max_retries=0
    )
    run = run_source_sync(source, fetcher=fetcher, **kwargs)
    source.refresh_from_db()
    return run


# ---------------------------------------------------------------------------------- discovery


@pytest.mark.django_db
def test_sitemap_discovery_finds_every_product_once_without_keyword_filters():
    source = shop_source()
    log: list[str] = []
    ctx = AdapterContext(
        source=source,
        fetcher=SafeFetcher(["shop.test"], transport=mock_site(store_pages(), log), min_delay=0, max_retries=0),
    )
    cfg = {
        "sitemap_url": f"{ORIGIN}/sitemap.xml",
        "sitemap_include": [r"sitemap_products_\d+\.xml"],
        "product_url_patterns": [r"^https://shop\.test/products/[^/.]+$"],
        "max_urls": 100,
    }
    result = discover_from_sitemaps(ctx, cfg)
    assert [i.external_id for i in result.items] == [
        f"{ORIGIN}/products/domo-2p",
        f"{ORIGIN}/products/ridge-w-fleece",
        f"{ORIGIN}/products/summit-30-mochila",
    ]
    assert result.complete and result.duplicates_collapsed == 4
    assert result.items[0].lastmod == "2026-11-20"
    assert not any("sitemap_pages" in url for url in log)  # non-product sitemaps are not read
    assert f"{ORIGIN}/sitemap_collections_1.xml" in result.other_sitemaps


@pytest.mark.django_db
def test_discovery_is_incomplete_when_a_child_sitemap_fails_or_cap_is_hit():
    source = shop_source()
    pages = store_pages()
    pages[f"{ORIGIN}/en/sitemap_products_1.xml?from=1&to=9"] = httpx2.Response(503)
    fetcher = SafeFetcher(["shop.test"], transport=mock_site(pages, []), min_delay=0, max_retries=0)
    cfg = {
        "sitemap_url": f"{ORIGIN}/sitemap.xml",
        "sitemap_include": ["sitemap_products_"],
        "product_url_patterns": [r"/products/[^/.]+$"],
        "max_urls": 100,
    }
    result = discover_from_sitemaps(AdapterContext(source=source, fetcher=fetcher), cfg)
    assert not result.complete and any("child sitemap not read" in n for n in result.notes)
    assert len(result.items) == 3  # what could be read is still returned

    capped = discover_from_sitemaps(AdapterContext(source=source, fetcher=fetcher), {**cfg, "max_urls": 2})
    assert not capped.complete and len(capped.items) == 2


# ---------------------------------------------------------------------------------- Shopify extraction


@pytest.mark.django_db
def test_storefront_extraction_keeps_metadata_variants_offers_and_images():
    source = shop_source()
    ctx = AdapterContext(source=source)
    url = f"{ORIGIN}/products/ridge-w-fleece"
    p = extract_from_storefront_json(storefront("ridge-w-fleece"), ctx, url)
    assert p.category == UNCATEGORIZED_SLUG and p.category_basis == "fallback"
    assert p.brand == "Synthetic Outdoor" and p.external_id == url and p.canonical_url == url
    assert p.tags == ["cortavientos", "mujer", "nueva-coleccion"]
    assert p.external_ids == {"shopify_product_id": "9100000000001", "shopify_handle": "ridge-w-fleece"}
    assert [v.sku for v in p.variants] == ["RDG-W_S_107", "RDG-W_M_107", "RDG-W_S_141"]
    assert [v.gtin for v in p.variants] == ["4006381333931", "4006381333948", ""]  # bad check digit dropped
    assert p.variants[0].color == "Antracita" and p.variants[0].size_label == "S"
    assert p.variants[0].options == {"Color": "Antracita", "Talla": "S"}
    assert p.variants[0].size_eu is None  # apparel sizes are never read as EU shoe sizes
    assert [(o.variant_sku, str(o.price), o.currency, o.availability) for o in p.offers] == [
        ("RDG-W_S_107", "86", "EUR", "out_of_stock"),
        ("RDG-W_M_107", "86", "EUR", "in_stock"),
        ("RDG-W_S_141", "86", "EUR", "in_stock"),
    ]
    assert p.offers[0].url == f"{url}?variant=91001"
    assert [i.alt for i in p.images] == ["#color_antracita", "#color_chocolate"]
    assert p.images[0].variant_skus == ["RDG-W_S_107", "RDG-W_M_107"]
    attrs = {a.name: a.value for a in p.source_attributes}
    assert attrs == {
        "Tejido": "WindShield 280 g/m2",
        "Peso": "450 g",
        "Tratamiento": "WR",
        "Capucha": "No",
        "vendor": "Synthetic Outdoor Co",
    }
    assert "aventuras" not in json.dumps(p.model_dump(mode="json"))  # no marketing prose stored
    assert p.summary == ""


def test_description_attributes_ignore_prose():
    html = "<p>Ideal para el invierno: cálida, ligera y muy cómoda para todo tipo de actividades al aire libre "
    html += "con amigos y familia durante muchos años sin ningún problema.</p><li>Peso: 300 g</li>"
    assert [(a.name, a.value) for a in description_attributes(html)] == [("Peso", "300 g")]


def test_classification_rules_fields_and_fallback():
    cfg = {
        "category_rules": [
            {"pattern": r"\bmochilas?\b", "category": "backpack"},
            {"pattern": "^(?=.*chaqueta)(?=.*impermeable)", "category": "hardshell_jacket", "field": "tags"},
        ]
    }
    assert classify(cfg, url="https://x/products/a", product_type="Mochilas").category == "backpack"
    assert classify(cfg, tags=["chaqueta", "impermeable"]).category == "hardshell_jacket"
    assert classify(cfg, name="chaqueta impermeable").category == UNCATEGORIZED_SLUG  # rule limited to tags
    fallback = classify(cfg, name="DOMO 2P", tags=["tiendas de campaña"])
    assert (fallback.category, fallback.basis) == (UNCATEGORIZED_SLUG, "fallback")
    assert classify({**cfg, "default_category": "midlayer"}, name="x").basis == "default"


def test_altus_rules_classify_conservatively():
    registry = load_registry(Path(settings.DATA_DIR) / "sources.yaml")
    altus = next(s for s in registry.sources if s.slug == "altus-website")
    cfg = altus.config
    assert altus.categories == [] and altus.adapter == "shopify_product"
    cases = {
        ("/products/mochila-trek-40", ""): "backpack",
        ("/products/plumifero-k2", ""): "insulated_jacket",
        ("/products/chaqueta-gore-tex-m", "impermeable"): "hardshell_jacket",
        ("/products/forro-polar-ander", ""): "midlayer",
        ("/products/saco-plumas-1000", ""): UNCATEGORIZED_SLUG,  # down sleeping bag is not a jacket
        ("/products/decan-w-h30", "cortavientos mujer"): UNCATEGORIZED_SLUG,
        ("/products/guantes-polar", ""): UNCATEGORIZED_SLUG,
        ("/products/bote-spray-impermeabilizante", "tiendas de campaña"): UNCATEGORIZED_SLUG,
    }
    for (path, tags), expected in cases.items():
        got = classify(cfg, url="https://wearealtus.com" + path, name=path.split("/")[-1].replace("-", " "), tags=tags)
        assert got.category == expected, (path, got)


# ---------------------------------------------------------------------------------- end-to-end


@pytest.mark.django_db
def test_full_catalog_sync_publishes_uncategorized_and_is_idempotent():
    source = shop_source()
    pages = store_pages()
    first = sync(source, pages)
    assert first.status == IngestionRun.Status.SUCCEEDED and first.catalog_complete and first.discovery_complete
    assert first.metrics["discovered"] == 3 and first.metrics["new"] == 3
    by_name = {p.name: p for p in Product.objects.select_related("category")}
    assert by_name["SUMMIT 30 MOCHILA"].category.slug == "backpack"  # rule on product type / collection
    assert by_name["SUMMIT 30 MOCHILA"].category_basis == "rule"
    assert by_name["SUMMIT 30 MOCHILA"].collections == ["mochilas"]
    ridge = by_name["RIDGE W FLEECE"]
    assert ridge.category.slug == UNCATEGORIZED_SLUG and ridge.category_basis == "fallback"
    assert ridge.publication_status == Product.PublicationStatus.PUBLISHED
    assert ridge.specifications == {} and ridge.tags == ["cortavientos", "mujer", "nueva-coleccion"]
    assert ridge.external_ids == {
        "shop-test": {"shopify_product_id": "9100000000001", "shopify_handle": "ridge-w-fleece"}
    }
    assert {a["source"] for a in ridge.source_attributes} == {"shop-test"}
    assert ProductVariant.objects.filter(product=ridge).count() == 3
    assert Offer.objects.filter(product=ridge).count() == 3
    # no review noise for unclassified products (nothing to validate), no invalid-value reviews
    assert not ReviewItem.objects.filter(kind=ReviewItem.Kind.INVALID_VALUE).exists()
    assert SpecificationEvidence.objects.filter(product=ridge).count() == 0

    counts = (
        Product.objects.count(),
        ProductVariant.objects.count(),
        Offer.objects.count(),
        SourceRecord.objects.count(),
    )
    second = sync(source, pages)
    assert second.status == IngestionRun.Status.SUCCEEDED
    assert second.metrics["not_modified"] == 3 and second.metrics["new"] == 0  # ETag on the product JSON
    third_pages = dict(pages)
    for handle in ("ridge-w-fleece", "summit-30-mochila", "domo-2p"):
        third_pages[f"{ORIGIN}/products/{handle}.js"] = product_js(handle)  # no ETag: full re-extraction
    third = sync(source, third_pages)
    assert third.metrics["unchanged"] == 3 and third.metrics["updated"] == 0
    after = (
        Product.objects.count(),
        ProductVariant.objects.count(),
        Offer.objects.count(),
        SourceRecord.objects.count(),
    )
    assert after == counts


@pytest.mark.django_db
def test_uncategorized_products_are_searchable_and_have_a_catalog_sheet(settings):
    settings.MI_INCLUDE_SYNTHETIC_DATA = False
    source = shop_source()
    sync(source, store_pages())
    found = search_products(query="cortavientos")
    assert [i["name"] for i in found["items"]] == ["RIDGE W FLEECE"]
    item = found["items"][0]
    assert item["category"] == UNCATEGORIZED_SLUG and item["technical_classification"] is False
    assert item["coverage"] == 0.0 and item["image_url"].startswith("https://cdn.shop.test/")
    assert any("UNCATEGORIZED" in w for w in found["warnings"])
    assert search_products(query="RDG-W_M_107")["items"][0]["name"] == "RIDGE W FLEECE"  # SKU lookup
    only_technical = search_products(classification="technical")
    assert {i["name"] for i in only_technical["items"]} == {"SUMMIT 30 MOCHILA"}
    only_uncat = search_products(classification="uncategorized")
    assert {i["name"] for i in only_uncat["items"]} == {"RIDGE W FLEECE", "DOMO 2P"}

    detail = product_detail(item["product_id"])
    assert detail["technical_classification"] is False and detail["specifications"] == []
    assert detail["category_basis"] == "fallback"
    assert {v["sku"] for v in detail["variants"]} == {"RDG-W_S_107", "RDG-W_M_107", "RDG-W_S_141"}
    variant = next(v for v in detail["variants"] if v["sku"] == "RDG-W_S_107")
    assert variant["options"] == {"Color": "Antracita", "Talla": "S"} and variant["gtin"] == "4006381333931"
    assert detail["availability"] == {"in_stock": 2, "out_of_stock": 1}
    assert {o["variant_sku"] for o in detail["offers"]} == {"RDG-W_M_107", "RDG-W_S_141"}
    assert {"name": "Peso", "value": "450 g", "unit": "", "source": "shop-test"} in detail["source_attributes"]
    assert any("not technically classified" in w for w in detail["warnings"])


@pytest.mark.django_db
def test_batched_cycle_is_resumable_and_only_complete_at_the_end():
    source = shop_source(batch_size=2)
    pages = store_pages()
    first = sync(source, pages)
    assert first.status == IngestionRun.Status.INCOMPLETE and not first.catalog_complete
    assert first.discovery_complete and first.metrics["processed"] == 2
    assert first.batch["total"] == 3 and first.batch["remaining_after"] == 1
    assert source.sync_state["cursor"] == f"{ORIGIN}/products/ridge-w-fleece"
    assert Product.objects.count() == 2
    assert source in due_sources()  # a cycle in progress is due again immediately

    second = sync(source, pages)
    assert second.status == IngestionRun.Status.SUCCEEDED and second.catalog_complete
    assert second.metrics["processed"] == 1 and second.batch["already_done"] == 2
    assert Product.objects.count() == 3
    assert "cursor" not in source.sync_state and source.sync_state["cycles_completed"] == 1
    assert source not in due_sources()  # the next full cycle waits sync_frequency_hours

    third = sync(source, pages)  # a new cycle starts from the beginning
    assert third.batch["already_done"] == 0 and third.metrics["processed"] == 2
    restarted = sync(source, pages, restart_cycle=True)
    assert restarted.batch["already_done"] == 0


@pytest.mark.django_db
def test_due_sources_respects_frequency_and_batch_interval():
    source = shop_source(batch_size=2, batch_interval_minutes=30)
    sync(source, store_pages())
    assert source not in due_sources()  # cycle in progress but within batch_interval_minutes
    assert source in due_sources(now=timezone.now() + timedelta(minutes=31))


@pytest.mark.django_db
def test_truncated_runs_never_flag_missing_products():
    source = shop_source()
    pages = store_pages()
    sync(source, pages)
    # the product disappears from the sitemaps, but one child sitemap cannot be read
    shrunk = store_pages(("ridge-w-fleece", "summit-30-mochila"))
    shrunk[f"{ORIGIN}/en/sitemap_products_1.xml?from=1&to=9"] = httpx2.Response(500)
    run = sync(source, shrunk)
    assert not run.discovery_complete and not run.catalog_complete
    assert run.status == IngestionRun.Status.INCOMPLETE and run.metrics["missing"] == 0
    assert any("discovery incomplete" in w for w in run.metrics["warnings"])
    domo = Product.objects.get(name="DOMO 2P")
    assert domo.source_status == Product.SourceStatus.OK
    # --max-items runs are never complete either
    limited_run = sync(source, store_pages(("ridge-w-fleece",)), max_items=1)
    assert limited_run.status == IngestionRun.Status.INCOMPLETE and limited_run.metrics["missing"] == 0
    assert limited_run.metrics["warnings"][0].startswith("bounded run")
    # a complete discovery flags it, and a later complete discovery that lists it again reactivates it
    gone = sync(source, store_pages(("ridge-w-fleece", "summit-30-mochila")))
    assert gone.metrics["missing"] == 1
    domo.refresh_from_db()
    assert domo.source_status == Product.SourceStatus.MISSING_AT_SOURCE
    back = sync(source, store_pages())
    assert back.metrics["reactivated"] == 1
    domo.refresh_from_db()
    assert domo.source_status == Product.SourceStatus.OK
    assert not ReviewItem.objects.filter(kind=ReviewItem.Kind.MISSING_AT_SOURCE, status="pending").exists()


@pytest.mark.django_db
def test_restricted_source_skips_out_of_scope_products():
    source = shop_source(categories=["backpack"])
    run = sync(source, store_pages())
    assert run.metrics["new"] == 1 and run.metrics["out_of_scope"] == 2
    assert run.status == IngestionRun.Status.SUCCEEDED
    assert list(Product.objects.values_list("name", flat=True)) == ["SUMMIT 30 MOCHILA"]
    assert SourceRecord.objects.count() == 1


@pytest.mark.django_db
def test_progressive_classification_upgrades_without_duplicates_and_never_downgrades():
    source = shop_source()
    pages = store_pages()
    sync(source, pages)
    ridge = Product.objects.get(name="RIDGE W FLEECE")
    public_id = ridge.public_id
    # A new rule classifies the product: same product, new technical category
    source.config["category_rules"].append({"pattern": "cortavientos", "category": "midlayer"})
    source.save()
    fresh = {k: (product_js(k.rsplit("/", 1)[-1][:-3]) if k.endswith(".js") else v) for k, v in pages.items()}
    run = sync(source, fresh)
    assert run.metrics["updated"] >= 1
    ridge.refresh_from_db()
    assert (ridge.category.slug, ridge.category_basis, ridge.public_id) == ("midlayer", "rule", public_id)
    assert Product.objects.filter(name="RIDGE W FLEECE").count() == 1
    # The rule disappears again: the technical category is kept (no downgrade, no conflict)
    source.config["category_rules"].pop()
    source.save()
    run = sync(source, fresh)
    ridge.refresh_from_db()
    assert ridge.category.slug == "midlayer" and run.metrics["conflicts"] == 0


@pytest.mark.django_db
def test_legacy_record_ids_are_rekeyed_to_canonical_urls():
    source = shop_source()
    sync(source, store_pages())
    record = SourceRecord.objects.get(external_id=f"{ORIGIN}/products/domo-2p")
    product_id = record.product_id
    SourceRecord.objects.filter(pk=record.pk).update(external_id=f"{ORIGIN}/en/products/domo-2p/", etag="")
    run = sync(source, store_pages())
    assert run.metrics["rekeyed"] == 1 and run.metrics["missing"] == 0
    record.refresh_from_db()
    assert record.external_id == f"{ORIGIN}/products/domo-2p" and record.product_id == product_id
    assert SourceRecord.objects.count() == 3 and Product.objects.count() == 3


@pytest.mark.django_db
def test_structured_uncategorized_import_keeps_raw_values_as_attributes(structured_source):
    source = structured_source(
        products=[
            {
                "external_id": "lamp-1",
                "brand": "Test Maker",
                "name": "Frontal 400",
                "category": "uncategorized",
                "source_category": "Iluminación > Frontales",
                "specifications": {"lumens": 400, "weight_g": 86},
                "tags": ["frontal"],
            }
        ]
    )
    run = run_source_sync(source)
    assert run.status == IngestionRun.Status.SUCCEEDED and run.metrics["new"] == 1
    product = Product.objects.get()
    assert product.category.slug == UNCATEGORIZED_SLUG and product.specifications == {}
    assert product.source_category == "Iluminación > Frontales"
    assert {(a["name"], a["value"]) for a in product.source_attributes} == {("lumens", "400"), ("weight_g", "86")}
    assert not ReviewItem.objects.exists()


# ---------------------------------------------------------------------------------- exclusions


@pytest.fixture
def mixed_catalog(demo_catalog, structured_source, import_dir):
    source = structured_source(
        slug="mixed-import",
        products=[
            {"external_id": "u1", "brand": "Test Maker", "name": "Saco Uno", "category": "uncategorized"},
            {"external_id": "u2", "brand": "Test Maker", "name": "Saco Dos", "category": "uncategorized"},
            {"external_id": "b1", "brand": "Test Maker", "name": "Bare Boot", "category": "mountaineering_boot"},
            {"external_id": "s1", "brand": "Test Maker", "name": "Bare Shoe", "category": "trail_running_shoe"},
        ],
    )
    run_source_sync(source)
    return {p.name: p.public_id for p in Product.objects.filter(brand__slug="test-maker")}


@pytest.mark.django_db
def test_uncategorized_is_excluded_from_technical_features(mixed_catalog):
    with pytest.raises(QueryValidationError, match="no technical classification"):
        recommend(activity="winter_mountaineering", category=UNCATEGORIZED_SLUG)
    with pytest.raises(QueryValidationError, match="no technical classification"):
        compare_products([mixed_catalog["Saco Uno"], mixed_catalog["Saco Dos"]])
    boot = mixed_catalog["Bare Boot"]
    with pytest.raises(QueryValidationError, match="no technical classification"):
        check_compatibility([boot, mixed_catalog["Saco Uno"]], include_synthetic=True)
    with pytest.raises(QueryValidationError, match="specification filters are not available"):
        search_products(category=UNCATEGORIZED_SLUG, filters=[{"field": "weight_g", "op": "lte", "value": 1}])


@pytest.mark.django_db
def test_products_without_technical_data_are_never_recommended(mixed_catalog):
    r = recommend(activity="winter_mountaineering", category="mountaineering_boot", include_synthetic=True)
    assert mixed_catalog["Bare Boot"] not in [i["product_id"] for i in r["recommended"]]
    # A category outside the profile has no mandatory criteria: only the technical-data guard applies
    r = recommend(activity="winter_mountaineering", category="trail_running_shoe", include_synthetic=True, limit=10)
    bare = mixed_catalog["Bare Shoe"]
    assert bare not in [i["product_id"] for i in r["recommended"]]
    verify = {i["product_id"]: i for i in r["requires_verification"]}
    assert any(c["criterion"] == "technical data" and c["result"] == "unknown" for c in verify[bare]["checks"])


# ---------------------------------------------------------------------------------- registry


def entry(**overrides) -> dict:
    base = {
        "slug": "x-site",
        "name": "X",
        "source_type": "manufacturer_website",
        "adapter": "shopify_product",
        "allowed_domains": ["x.test"],
    }
    base.update(overrides)
    return base


def test_registry_accepts_unrestricted_sources_and_validates_config():
    assert SourceEntry.model_validate(entry(categories=[])).categories == []
    with pytest.raises(ValueError, match="outside `categories`"):
        SourceEntry.model_validate(
            entry(categories=["backpack"], config={"category_rules": [{"pattern": "x", "category": "crampon"}]})
        )
    with pytest.raises(ValueError, match="unknown category"):
        SourceEntry.model_validate(entry(config={"category_rules": [{"pattern": "x", "category": "tent"}]}))
    with pytest.raises(ValueError, match="invalid regex"):
        SourceEntry.model_validate(entry(config={"product_url_patterns": ["("]}))
    with pytest.raises(ValueError, match="currency"):
        SourceEntry.model_validate(entry(config={"capture_offers": True}))
    with pytest.raises(ValueError, match="allowed_domains"):
        SourceEntry.model_validate(entry(allowed_domains=[]))
    with pytest.raises(ValueError, match="non-negative integer"):
        SourceEntry.model_validate(entry(config={"batch_size": "100"}))
    assert Registry.model_validate({"version": 1, "sources": [entry(categories=["uncategorized"])]})


@pytest.mark.django_db
def test_manual_classification_is_validated_and_audited(mixed_catalog):
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    saco = mixed_catalog["Saco Uno"]
    call_command("classify_product", saco, "backpack", "--actor", "alice", "--reason", "checked", stdout=io.StringIO())
    product = Product.objects.get(public_id=saco)
    assert (product.category.slug, product.category_basis) == ("backpack", "review")
    audit = ReviewItem.objects.get(kind=ReviewItem.Kind.RECLASSIFICATION, product=product)
    assert (audit.current_value, audit.proposed_value, audit.decided_by) == ("uncategorized", "backpack", "alice")
    with pytest.raises(CommandError, match="already"):
        call_command("classify_product", saco, "backpack", "--actor", "a", "--reason", "r", stdout=io.StringIO())
