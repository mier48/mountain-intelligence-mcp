"""Generic schema.org JSON-LD product adapter (sitemap or explicit URL discovery).

Source config keys (all optional unless stated):

- ``discovery``: ``"sitemap"`` (default) or ``"urls"``
- ``sitemap_url``: sitemap or sitemap index URL (sitemap discovery)
- ``urls``: explicit product URLs (urls discovery)
- ``url_include``: regexes; only matching URLs are kept
- ``max_urls``: hard cap of discovered URLs (default 200)
- ``default_category`` / ``category_rules``: ``[{"pattern": regex, "category": slug}]`` on the URL
- ``property_map``: ``{"<additionalProperty name>": {"field": spec_field, "unit": "g"}}``
- ``capture_offers``: store schema.org offers as price observations (default false)

Only structured metadata is used. Descriptions and marketing copy are not stored.
"""

from __future__ import annotations

import html
import json
import re
import zlib
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from ..contracts import (
    DiscoveredItem,
    ExtractedOffer,
    ExtractedProduct,
    ExtractedVariant,
    FetchedDocument,
    FieldLocator,
    gtin_is_valid,
)
from ..http import FetchError
from .base import AdapterContext, AdapterError, register

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_AVAILABILITY = {
    "instock": "in_stock",
    "outofstock": "out_of_stock",
    "preorder": "preorder",
    "presale": "preorder",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
    "limitedavailability": "in_stock",
}
MAX_SITEMAP_DOCS = 20
# Sitemap protocol limit (50 MB uncompressed per file); product pages keep INGESTION_MAX_RESPONSE_BYTES.
MAX_SITEMAP_BYTES = 50 * 1024 * 1024


def _types(node: dict[str, Any]) -> set[str]:
    t = node.get("@type")
    values = t if isinstance(t, list) else [t]
    return {str(v).rsplit("/", 1)[-1] for v in values if v}


def iter_jsonld_nodes(html_text: str) -> Iterator[dict[str, Any]]:
    """Yield every JSON object found in ld+json scripts (flattening @graph and arrays)."""
    tree = LexborHTMLParser(html_text)
    for script in tree.css('script[type="application/ld+json"]'):
        text = script.text(deep=True, strip=False) or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, dict):
                if "@graph" in current:
                    stack.append(current["@graph"])
                if current.get("@type"):
                    yield current


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return " ".join(html.unescape(str(value or "")).split())


def _first_gtin(node: dict[str, Any]) -> str:
    for key in ("gtin14", "gtin13", "gtin12", "gtin8", "gtin"):
        code = re.sub(r"\D", "", str(node.get(key) or ""))
        if code and gtin_is_valid(code):
            return code
    return ""


def _quantity(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, dict):
        return value.get("value"), value.get("unitCode") or value.get("unitText")
    return value, None


class JsonLdProductAdapter:
    key = "jsonld_product"
    needs_network = True

    # -- discovery ---------------------------------------------------------------------------
    def discover(self, ctx: AdapterContext) -> Iterable[DiscoveredItem]:
        cfg = ctx.source.config
        include = [re.compile(p) for p in cfg.get("url_include", [])]
        max_urls = min(int(cfg.get("max_urls", 200)), 5000)
        if cfg.get("discovery", "sitemap") == "urls":
            urls: Iterable[str] = cfg.get("urls", [])
        else:
            if not cfg.get("sitemap_url"):
                raise AdapterError("sitemap discovery requires config.sitemap_url")
            urls = self._sitemap_urls(str(cfg["sitemap_url"]), ctx)
        seen: set[str] = set()
        for url in urls:
            if include and not any(p.search(url) for p in include):
                continue
            if url in seen:
                continue
            seen.add(url)
            yield DiscoveredItem(external_id=url, url=url)
            if len(seen) >= max_urls:
                ctx.warnings.append(f"discovery capped at max_urls={max_urls}")
                return

    def _sitemap_urls(self, sitemap_url: str, ctx: AdapterContext) -> Iterator[str]:
        assert ctx.fetcher is not None
        pending, fetched = [sitemap_url], 0
        while pending and fetched < MAX_SITEMAP_DOCS:
            url = pending.pop(0)
            fetched += 1
            doc = ctx.fetcher.fetch(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.5", max_bytes=MAX_SITEMAP_BYTES)
            body = doc.body
            if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
                # Bounded decompression: protects against gzip bombs.
                body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, MAX_SITEMAP_BYTES)
            text = body.decode("utf-8", errors="replace")
            locs = [html.unescape(m) for m in _LOC_RE.findall(text)]
            if "<sitemapindex" in text[:2000].lower():
                pending.extend(locs)
                continue
            yield from locs

    # -- fetch -------------------------------------------------------------------------------
    def fetch(
        self, item: DiscoveredItem, ctx: AdapterContext, *, etag: str = "", last_modified: str = ""
    ) -> FetchedDocument:
        if ctx.fetcher is None:  # pragma: no cover - pipeline always provides one
            raise AdapterError("network adapter requires a fetcher")
        try:
            return ctx.fetcher.fetch(item.url, etag=etag, last_modified=last_modified, item=item)
        except FetchError:
            raise

    # -- extraction --------------------------------------------------------------------------
    def extract(self, document: FetchedDocument, ctx: AdapterContext) -> list[ExtractedProduct]:
        if "html" not in document.content_type and not document.body.lstrip().startswith(b"<"):
            raise AdapterError(f"unexpected content type {document.content_type!r}")
        nodes = list(iter_jsonld_nodes(document.body.decode("utf-8", errors="replace")))
        groups = [n for n in nodes if "ProductGroup" in _types(n)]
        products = [n for n in nodes if "Product" in _types(n)]
        main = groups[0] if groups else (products[0] if products else None)
        if main is None:
            raise AdapterError("no schema.org Product found in JSON-LD")
        return [self._to_contract(main, document, ctx)]

    def _category(self, url: str, node: dict[str, Any], ctx: AdapterContext) -> str:
        cfg = ctx.source.config
        haystack = f"{url} {_text(node.get('category'))}"
        for rule in cfg.get("category_rules", []):
            if re.search(rule["pattern"], haystack, re.IGNORECASE):
                return str(rule["category"])
        if cfg.get("default_category"):
            return str(cfg["default_category"])
        raise AdapterError("cannot determine category (configure default_category or category_rules)")

    def _to_contract(self, node: dict[str, Any], doc: FetchedDocument, ctx: AdapterContext) -> ExtractedProduct:
        cfg = ctx.source.config
        url = _text(node.get("url")) or doc.url
        property_map: dict[str, dict[str, str]] = {k.casefold(): v for k, v in cfg.get("property_map", {}).items()}
        specs: dict[str, Any] = {}
        locators: dict[str, FieldLocator] = {}

        raw_weight, weight_unit = _quantity(node.get("weight"))
        if raw_weight not in (None, ""):
            specs["weight_g"] = str(raw_weight)
            locators["weight_g"] = FieldLocator(
                locator="jsonld:$.weight",
                unit=weight_unit,
                excerpt=f"weight: {raw_weight} {weight_unit or ''}".strip()[:280],
            )

        for prop in node.get("additionalProperty") or []:
            if not isinstance(prop, dict):
                continue
            name = _text(prop.get("name"))
            mapping = property_map.get(name.casefold())
            if not mapping:
                continue
            value = prop.get("value")
            unit = prop.get("unitCode") or prop.get("unitText") or mapping.get("unit")
            target = mapping["field"]
            specs[target] = value if isinstance(value, (bool, int, float)) else _text(value)
            locators[target] = FieldLocator(
                locator=f"jsonld:$.additionalProperty[?(@.name=={name!r})]",
                unit=unit,
                excerpt=f"{name}: {_text(value)} {prop.get('unitText') or ''}".strip()[:280],
            )

        variants: list[ExtractedVariant] = []
        for variant in (node.get("hasVariant") or [])[:500]:
            if not isinstance(variant, dict):
                continue
            try:
                variants.append(
                    ExtractedVariant(
                        sku=_text(variant.get("sku"))[:80],
                        mpn=_text(variant.get("mpn"))[:80],
                        gtin=_first_gtin(variant),
                        size_label=_text(variant.get("size"))[:40],
                        color=_text(variant.get("color"))[:80],
                    )
                )
            except ValidationError:
                ctx.warnings.append("skipped an invalid variant")

        offers: list[ExtractedOffer] = []
        if cfg.get("capture_offers"):
            offers = self._offers(node, url, ctx)

        brand = _text(node.get("brand")) or (ctx.source.brand.name if ctx.source.brand else "")
        external = _text(node.get("productGroupID")) or _text(node.get("sku")) or _text(node.get("mpn")) or url
        try:
            return ExtractedProduct(
                external_id=external[:300],
                canonical_url=url[:1000],
                brand=brand[:120],
                brand_domain=(ctx.source.brand.official_domain if ctx.source.brand else ""),
                name=_text(node.get("name"))[:200],
                category=self._category(url, node, ctx),
                mpn=_text(node.get("mpn"))[:80],
                specifications=specs,
                locators=locators,
                variants=variants,
                offers=offers,
            )
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise AdapterError(f"incomplete product metadata: {', '.join(fields)}") from exc

    def _offers(self, node: dict[str, Any], url: str, ctx: AdapterContext) -> list[ExtractedOffer]:
        raw = node.get("offers")
        items = raw if isinstance(raw, list) else [raw] if raw else []
        out: list[ExtractedOffer] = []
        for offer in items[:50]:
            if not isinstance(offer, dict) or "AggregateOffer" in _types(offer):
                continue  # aggregate ranges are not a concrete price
            try:
                price = Decimal(str(offer.get("price")))
                availability = _AVAILABILITY.get(
                    str(offer.get("availability", "")).rsplit("/", 1)[-1].lower(), "unknown"
                )
                out.append(
                    ExtractedOffer(
                        retailer=ctx.source.name[:120],
                        url=_text(offer.get("url")) or url,
                        currency=str(offer.get("priceCurrency", "")).upper(),
                        price=price,
                        availability=availability,  # type: ignore[arg-type]
                    )
                )
            except (InvalidOperation, ValidationError, TypeError):
                ctx.warnings.append("skipped an invalid offer")
        return out


register(JsonLdProductAdapter())


def extract_from_html(html_text: str, ctx: AdapterContext, url: str = "https://example.invalid/") -> ExtractedProduct:
    """Convenience for tests and operator dry-runs on local files."""
    doc = FetchedDocument(
        item=DiscoveredItem(external_id=url, url=url),
        url=url,
        content_type="text/html",
        body=html_text.encode(),
        fetched_at=datetime.now(UTC),
    )
    return JsonLdProductAdapter().extract(doc, ctx)[0]
