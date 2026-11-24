"""Generic schema.org JSON-LD product adapter (sitemap or explicit URL discovery).

Source config keys (all optional unless stated):

- ``discovery``: ``"sitemap"`` (default) or ``"urls"``
- ``sitemap_url``: sitemap or sitemap index URL (sitemap discovery, see ``ingestion.discovery``
  for ``sitemap_include``, ``product_url_patterns``, ``url_exclude``, ``max_sitemaps`` and URL
  canonicalization)
- ``urls``: explicit product URLs (urls discovery)
- ``url_include``: legacy structural URL regexes; only matching URLs are kept
- ``max_urls``: safety cap of discovered URLs (default 200; hitting it makes discovery incomplete)
- ``default_category`` / ``category_rules``: see ``adapters.classification``; rules match the URL +
  JSON-LD category by default. No match and no default -> ``uncategorized``.
- ``property_map``: ``{"<additionalProperty name>": {"field": spec_field, "unit": "g"}}``
- ``capture_offers``: store schema.org offers as price observations (default false)

Only structured metadata is used. Descriptions and marketing copy are not stored. Every
``additionalProperty`` is preserved as an original ``source_attribute``; only mapped ones become
normalized specification candidates.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from ..contracts import (
    DiscoveredItem,
    ExtractedImage,
    ExtractedOffer,
    ExtractedProduct,
    ExtractedVariant,
    FetchedDocument,
    FieldLocator,
    SourceAttribute,
    gtin_is_valid,
)
from ..discovery import canonicalize_url, discover_from_sitemaps
from ..http import FetchError
from .base import AdapterContext, AdapterError, register
from .classification import classify

_AVAILABILITY = {
    "instock": "in_stock",
    "outofstock": "out_of_stock",
    "preorder": "preorder",
    "presale": "preorder",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
    "limitedavailability": "in_stock",
}


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
    def canonical_key(self, external_id: str, cfg: dict[str, Any]) -> str:
        return canonicalize_url(external_id, cfg) if external_id.startswith(("http://", "https://")) else external_id

    def discover(self, ctx: AdapterContext) -> Iterable[DiscoveredItem]:
        cfg = ctx.source.config
        if cfg.get("discovery", "sitemap") == "urls":
            include = [re.compile(p) for p in cfg.get("url_include", [])]
            max_urls = min(int(cfg.get("max_urls", 200)), 50_000)
            seen: set[str] = set()
            for raw in cfg.get("urls", []):
                url = canonicalize_url(str(raw), cfg)
                if (include and not any(p.search(url) for p in include)) or url in seen:
                    continue
                if len(seen) >= max_urls:
                    ctx.discovery_complete = False
                    ctx.warnings.append(f"discovery capped at max_urls={max_urls}")
                    return
                seen.add(url)
                yield DiscoveredItem(external_id=url, url=url)
            return
        if not cfg.get("sitemap_url") and not cfg.get("sitemap_urls"):
            raise AdapterError("sitemap discovery requires config.sitemap_url")
        result = discover_from_sitemaps(ctx, cfg)
        ctx.record_discovery(result)
        yield from result.items

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

    def _to_contract(self, node: dict[str, Any], doc: FetchedDocument, ctx: AdapterContext) -> ExtractedProduct:
        cfg = ctx.source.config
        raw_url = _text(node.get("url"))
        url = canonicalize_url(raw_url if raw_url.startswith(("http://", "https://")) else doc.url, cfg)
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
                size, color = _text(variant.get("size"))[:40], _text(variant.get("color"))[:80]
                images = jsonld_images(variant)
                variants.append(
                    ExtractedVariant(
                        sku=_text(variant.get("sku"))[:80],
                        mpn=_text(variant.get("mpn"))[:80],
                        gtin=_first_gtin(variant),
                        size_label=size,
                        color=color,
                        options={k: v for k, v in (("size", size), ("color", color)) if v},
                        image_url=images[0].url if images else "",
                    )
                )
            except ValidationError:
                ctx.warnings.append("skipped an invalid variant")

        offers: list[ExtractedOffer] = []
        if cfg.get("capture_offers"):
            offers = self._offers(node, url, ctx)

        brand = _text(node.get("brand")) or (ctx.source.brand.name if ctx.source.brand else "")
        external = _text(node.get("productGroupID")) or _text(node.get("sku")) or _text(node.get("mpn")) or url
        name = _text(node.get("name"))[:200]
        source_category = _source_category(node)
        classification = classify(
            cfg, default_field="url_and_category", url=url, name=name, source_category=source_category
        )
        external_ids = {
            k: v[:300]
            for k, v in (
                ("jsonld_product_group_id", _text(node.get("productGroupID"))),
                ("jsonld_sku", _text(node.get("sku"))),
                ("jsonld_mpn", _text(node.get("mpn"))),
            )
            if v
        }
        try:
            return ExtractedProduct(
                external_id=external[:300],
                canonical_url=url[:1000],
                brand=brand[:120],
                brand_domain=(ctx.source.brand.official_domain if ctx.source.brand else ""),
                name=name,
                category=classification.category,
                category_basis=classification.basis,  # type: ignore[arg-type]
                mpn=_text(node.get("mpn"))[:80],
                specifications=specs,
                locators=locators,
                variants=variants,
                offers=offers,
                source_category=source_category,
                external_ids=external_ids,
                images=jsonld_images(node),
                source_attributes=jsonld_attributes(node),
            )
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise AdapterError(f"incomplete product metadata: {', '.join(fields)}") from exc

    def _offers(self, node: dict[str, Any], url: str, ctx: AdapterContext) -> list[ExtractedOffer]:
        out = self._node_offers(node, url, ctx, variant_sku="")
        for variant in (node.get("hasVariant") or [])[:500]:
            if isinstance(variant, dict):
                out.extend(self._node_offers(variant, url, ctx, variant_sku=_text(variant.get("sku"))[:80]))
        return out[:500]

    def _node_offers(
        self, node: dict[str, Any], url: str, ctx: AdapterContext, *, variant_sku: str
    ) -> list[ExtractedOffer]:
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
                        variant_sku=variant_sku,
                    )
                )
            except (InvalidOperation, ValidationError, TypeError):
                ctx.warnings.append("skipped an invalid offer")
        return out


def _source_category(node: dict[str, Any]) -> str:
    """Manufacturer category as declared in JSON-LD (text, list or breadcrumb-like value)."""
    value = node.get("category")
    if isinstance(value, list):
        return " > ".join(t for t in (_text(v) for v in value) if t)[:300]
    return _text(value)[:300]


def jsonld_images(node: dict[str, Any], limit: int = 50) -> list[ExtractedImage]:
    raw = node.get("image")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    out: list[ExtractedImage] = []
    seen: set[str] = set()
    for position, item in enumerate(items[:limit], start=1):
        url = item.get("url") or item.get("contentUrl") if isinstance(item, dict) else item
        url = str(url or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")) or url in seen or len(url) > 1000:
            continue
        seen.add(url)
        alt = _text(item.get("caption") or item.get("name")) if isinstance(item, dict) else ""
        out.append(ExtractedImage(url=url, alt=alt[:200], position=position))
    return out


def jsonld_attributes(node: dict[str, Any], limit: int = 150) -> list[SourceAttribute]:
    """Every schema.org additionalProperty (plus weight/material/color) as declared by the source."""
    out: list[SourceAttribute] = []
    candidates: list[tuple[str, Any, str, str]] = []
    weight, weight_unit = _quantity(node.get("weight"))
    if weight not in (None, ""):
        candidates.append(("weight", weight, str(weight_unit or ""), "jsonld:$.weight"))
    for key in ("material", "color", "pattern", "audience", "model"):
        if node.get(key):
            candidates.append((key, node.get(key), "", f"jsonld:$.{key}"))
    for prop in node.get("additionalProperty") or []:
        if isinstance(prop, dict):
            name = _text(prop.get("name"))
            unit = str(prop.get("unitText") or prop.get("unitCode") or "")
            candidates.append((name, prop.get("value"), unit, f"jsonld:$.additionalProperty[?(@.name=={name!r})]"))
    for name, value, unit, locator in candidates:
        text_value = _text(value) if not isinstance(value, (bool, int, float)) else str(value)
        if not name or not text_value:
            continue
        try:
            out.append(SourceAttribute(name=name[:120], value=text_value[:500], unit=unit[:20], locator=locator[:300]))
        except ValidationError:
            continue
        if len(out) >= limit:
            break
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
