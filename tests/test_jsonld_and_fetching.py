"""JSON-LD extraction, network adapter pipeline and SSRF / fetching safety."""

from __future__ import annotations

from pathlib import Path

import httpcore2
import httpx2
import pytest
from django.conf import settings

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.ingestion.adapters.base import AdapterContext, AdapterError
from mountain_intelligence.ingestion.adapters.jsonld import extract_from_html
from mountain_intelligence.ingestion.http import (
    FetchError,
    GuardedNetworkBackend,
    SafeFetcher,
    UrlPolicy,
    is_public_address,
)
from mountain_intelligence.ingestion.models import IngestionRun, ReviewItem
from mountain_intelligence.ingestion.pipeline import run_source_sync
from mountain_intelligence.sources.models import Source, SourceRecord

FIXTURES = Path(settings.FIXTURES_DIR) / "jsonld"
PROPERTY_MAP = {
    "Peso por": {"field": "weight_basis"},
    "Compatibilidad crampones": {"field": "crampon_attachment_types"},
    "Membrana": {"field": "membrane"},
    "Binding": {"field": "binding_types"},
    "Points": {"field": "points"},
}


def source_obj(**config) -> Source:
    return Source(
        slug="maker",
        name="Maker",
        source_type=Source.SourceType.MANUFACTURER_WEBSITE,
        adapter="jsonld_product",
        allowed_domains=["www.maker.test"],
        access_status=Source.AccessStatus.AUTHORIZED,
        enabled=True,
        config={"property_map": PROPERTY_MAP, **config},
    )


# ------------------------------------------------------------------------------ extraction


def test_extracts_product_group_with_units_and_variants():
    ctx = AdapterContext(source=source_obj(default_category="mountaineering_boot"))
    extracted = extract_from_html((FIXTURES / "product_group.html").read_text(), ctx)
    assert extracted.external_id == "SM-ALP-01"
    assert extracted.brand == "Synthetic Maker"
    assert extracted.specifications == {
        "weight_g": "1,62",
        "weight_basis": "par",
        "crampon_attachment_types": "Semiautomático, Automático",
        "membrane": "Synthetic-Tex",
    }
    assert extracted.locators["weight_g"].unit == "KGM"
    assert [v.sku for v in extracted.variants] == ["SM-ALP-01-42", "SM-ALP-01-43"]
    # invalid GTIN check digit is dropped, valid one kept
    assert [v.gtin for v in extracted.variants] == ["4006381333931", ""]
    assert extracted.summary == ""  # marketing description is never stored
    assert extracted.offers == []  # offers only with capture_offers


def test_extracts_list_and_skips_aggregate_offer_and_bad_json():
    ctx = AdapterContext(
        source=source_obj(category_rules=[{"pattern": "crampon", "category": "crampon"}], capture_offers=True)
    )
    extracted = extract_from_html((FIXTURES / "simple_product.html").read_text(), ctx)
    assert extracted.category == "crampon"
    assert extracted.specifications["binding_types"] == "step-in"
    assert extracted.offers == []


def test_page_without_product_is_an_extraction_error():
    ctx = AdapterContext(source=source_obj(default_category="crampon"))
    with pytest.raises(AdapterError, match=r"no schema\.org Product"):
        extract_from_html((FIXTURES / "no_product.html").read_text(), ctx)


# ------------------------------------------------------------------------------ pipeline over HTTP


def mock_site(pages: dict[str, httpx2.Response], log: list[str]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        log.append(str(request.url))
        response = pages.get(str(request.url))
        if callable(response):
            return response(request)
        return response or httpx2.Response(404)

    return httpx2.MockTransport(handler)


SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.maker.test/products/synthetic-alpine-boot</loc></url>
<url><loc>https://www.maker.test/about</loc></url>
<url><loc>https://www.maker.test/products/gone</loc></url></urlset>"""


@pytest.mark.django_db
def test_sitemap_jsonld_sync_with_etag_and_404(structured_source):
    html = (FIXTURES / "product_group.html").read_bytes()
    etag = '"v1"'

    def product_page(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        return httpx2.Response(200, content=html, headers={"content-type": "text/html", "etag": etag})

    pages = {
        "https://www.maker.test/robots.txt": httpx2.Response(200, text="User-agent: *\nDisallow: /private\n"),
        "https://www.maker.test/sitemap.xml": httpx2.Response(
            200, content=SITEMAP, headers={"content-type": "application/xml"}
        ),
        "https://www.maker.test/products/synthetic-alpine-boot": product_page,
    }
    log: list[str] = []
    source = source_obj(
        discovery="sitemap",
        sitemap_url="https://www.maker.test/sitemap.xml",
        url_include=["/products/"],
        default_category="mountaineering_boot",
    )
    source.save()
    fetcher = SafeFetcher(source.allowed_domains, transport=mock_site(pages, log), min_delay=0, max_retries=0)
    run = run_source_sync(source, fetcher=fetcher)
    assert run.metrics["discovered"] == 2  # /about filtered by url_include
    assert run.metrics["new"] == 1
    assert run.metrics["errors"] == 1  # /products/gone -> 404
    product = Product.objects.get()
    assert product.specifications["weight_g"] == 1620.0
    assert product.specifications["weight_basis"] == "per_pair"
    assert product.specifications["crampon_attachment_types"] == ["semi_automatic", "automatic"]
    assert product.derived_specifications == {"weight_per_boot_g": 810.0}
    assert product.publication_status == Product.PublicationStatus.IN_REVIEW  # auto_publish is off
    assert ReviewItem.objects.filter(kind="new_product").count() == 1
    assert "Color favorito" not in str(product.specifications)

    # second run: conditional request -> 304 -> not modified, nothing rewritten
    run2 = run_source_sync(source, fetcher=fetcher)
    assert run2.metrics["not_modified"] == 1
    assert SourceRecord.objects.get(external_id="https://www.maker.test/products/synthetic-alpine-boot").etag == etag

    # the page disappears: product is kept and flagged
    pages["https://www.maker.test/products/synthetic-alpine-boot"] = httpx2.Response(404)
    run3 = run_source_sync(source, fetcher=fetcher)
    assert run3.status == IngestionRun.Status.PARTIAL
    product.refresh_from_db()
    assert product.source_status == Product.SourceStatus.MISSING_AT_SOURCE
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_discovery_failure_marks_run_failed_without_touching_catalog():
    pages = {"https://www.maker.test/robots.txt": httpx2.Response(200, text="User-agent: *\nDisallow: /\n")}
    source = source_obj(discovery="sitemap", sitemap_url="https://www.maker.test/sitemap.xml")
    source.save()
    fetcher = SafeFetcher(source.allowed_domains, transport=mock_site(pages, []), min_delay=0, max_retries=0)
    run = run_source_sync(source, fetcher=fetcher)
    assert run.status == IngestionRun.Status.FAILED
    assert run.errors[0]["kind"] == "robots"


# ------------------------------------------------------------------------------ SSRF & fetch limits


@pytest.mark.parametrize(
    "url",
    [
        "ftp://www.maker.test/x",
        "https://user:pw@www.maker.test/",
        "https://www.maker.test:8443/",
        "https://evil.test/",
        "https://www.maker.test.evil.test/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ],
)
def test_url_policy_blocks(url):
    with pytest.raises(FetchError) as exc:
        UrlPolicy(["www.maker.test", "127.0.0.1"]).check(url)
    assert exc.value.kind == "blocked"


def test_url_policy_allows_subdomains_of_allowlisted_domain():
    UrlPolicy(["maker.test"]).check("https://www.maker.test/products/x")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("2606:4700:4700::1111", True),
        ("224.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_public_address_classification(address, public):
    assert is_public_address(address) is public


def test_network_backend_blocks_private_resolution_dns_rebinding():
    backend = GuardedNetworkBackend(resolver=lambda host, port: ["93.184.216.34", "10.0.0.7"])
    with pytest.raises(httpcore2.ConnectError, match="non-public"):
        backend.connect_tcp("www.maker.test", 443)
    backend = GuardedNetworkBackend(resolver=lambda host, port: ["127.0.0.1"])
    with pytest.raises(httpcore2.ConnectError, match="non-public"):
        backend.connect_tcp("localhost.maker.test", 80)


def test_guarded_transport_end_to_end_blocks_private_resolution():
    from mountain_intelligence.ingestion.http import GuardedTransport

    fetcher = SafeFetcher(
        ["www.maker.test"],
        transport=GuardedTransport(resolver=lambda h, p: ["192.168.0.10"]),
        min_delay=0,
        max_retries=0,
        respect_robots=False,
    )
    with pytest.raises(FetchError) as exc:
        fetcher.fetch("https://www.maker.test/")
    assert exc.value.kind == "blocked"


def test_redirects_are_revalidated():
    pages = {
        "https://www.maker.test/a": httpx2.Response(302, headers={"location": "https://www.maker.test/b"}),
        "https://www.maker.test/b": httpx2.Response(301, headers={"location": "http://169.254.169.254/latest"}),
        "https://www.maker.test/c": httpx2.Response(302, headers={"location": "https://evil.test/"}),
    }
    log: list[str] = []
    fetcher = SafeFetcher(
        ["www.maker.test"], transport=mock_site(pages, log), min_delay=0, max_retries=0, respect_robots=False
    )
    for url in ("https://www.maker.test/a", "https://www.maker.test/c"):
        with pytest.raises(FetchError) as exc:
            fetcher.fetch(url)
        assert exc.value.kind == "blocked"
    assert not any("169.254" in u or "evil" in u for u in log)


def test_redirect_loop_is_bounded():
    pages = {"https://www.maker.test/a": httpx2.Response(302, headers={"location": "/a"})}
    fetcher = SafeFetcher(
        ["www.maker.test"],
        transport=mock_site(pages, []),
        min_delay=0,
        max_retries=0,
        respect_robots=False,
        max_redirects=3,
    )
    with pytest.raises(FetchError, match="too many redirects"):
        fetcher.fetch("https://www.maker.test/a")


def test_response_size_limit():
    pages = {"https://www.maker.test/big": httpx2.Response(200, content=b"x" * 2048)}
    fetcher = SafeFetcher(
        ["www.maker.test"],
        transport=mock_site(pages, []),
        min_delay=0,
        max_retries=0,
        respect_robots=False,
        max_bytes=1024,
    )
    with pytest.raises(FetchError) as exc:
        fetcher.fetch("https://www.maker.test/big")
    assert exc.value.kind == "too_large"
    # A per-call limit (used for sitemaps) can be larger than the fetcher's default.
    assert len(fetcher.fetch("https://www.maker.test/big", max_bytes=4096).body) == 2048


def test_retries_with_backoff_then_success():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx2.Response(503, headers={"retry-after": "1"})
        return httpx2.Response(200, text="ok")

    sleeps: list[float] = []
    fetcher = SafeFetcher(
        ["www.maker.test"],
        transport=mock_site({"https://www.maker.test/": flaky}, []),
        min_delay=0,
        max_retries=3,
        respect_robots=False,
        sleep=sleeps.append,
    )
    doc = fetcher.fetch("https://www.maker.test/")
    assert doc.body == b"ok" and calls["n"] == 3 and sleeps == [1.0, 1.0]


def test_robots_unavailable_means_no_crawl():
    pages = {"https://www.maker.test/robots.txt": httpx2.Response(500)}
    fetcher = SafeFetcher(["www.maker.test"], transport=mock_site(pages, []), min_delay=0, max_retries=0)
    with pytest.raises(FetchError) as exc:
        fetcher.fetch("https://www.maker.test/products/x")
    assert exc.value.kind == "robots"


def test_fetcher_requires_allowlist():
    with pytest.raises(ValueError):
        SafeFetcher([])
