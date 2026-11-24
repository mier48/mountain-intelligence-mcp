"""Reusable adapter for manufacturer storefronts running on Shopify.

Discovery reads the store's product sitemaps of the PRIMARY locale (every product page, no keyword
filters) and canonicalizes URLs so collection paths and parameters collapse into one item per
product. Locale copies (``/en/sitemap_products_1.xml``) are not read: Shopify stores may translate
handles per language (``/en/products/polyamide-chisel`` vs ``/products/cincel-poliamida``), so a
translated URL cannot be mapped to the primary one by stripping the prefix, and the primary
sitemap already lists every product once. For each product it reads the public storefront product JSON
(``/products/<handle>.js``), which carries the data the HTML only shows partially: product type,
tags, vendor, options, every variant with SKU, barcode (GTIN), price and availability, and images.
Optionally it also reads the product page's schema.org JSON-LD (technical ``additionalProperty``
values, manufacturer category) and the store's collection memberships.

Source config keys (all optional):

- ``sitemap_url``: defaults to ``<base_url>/sitemap.xml``
- ``sitemap_include``: child sitemaps to read (default: primary-locale product sitemaps
  ``^https?://[^/]+/sitemap_products_\\d+\\.xml``)
- ``product_url_patterns``: default ``^https?://[^/]+/products/[^/.]+$``
- ``url_exclude``: URLs to drop (e.g. gift cards)
- ``max_urls`` (default 20000) / ``max_sitemaps`` (default 50): safety caps; hitting one makes
  discovery incomplete (the run is never reported as a complete synchronization)
- ``brand_from``: ``source`` (default when the source has a brand) or ``vendor`` (multi-brand stores)
- ``category_rules`` / ``default_category``: see ``adapters.classification``; rules match URL, name,
  product type, tags and collections by default. Unmatched products are ``uncategorized``.
- ``property_map``: like the JSON-LD adapter, maps declared attribute names to schema fields
- ``capture_offers`` + ``currency`` (ISO 4217, required to capture prices): one offer per variant
- ``jsonld``: also read the product page JSON-LD (one extra request per product; default false)
- ``description_attributes``: keep ``Name: value`` lines of the product description as original
  attributes (no prose is stored; default false)
- ``collections``: ``{enabled: bool, include: [regex], max_collections: int, cache_hours: int}``
  collection membership as a classification signal (cached between runs)

The description itself is never stored (marketing copy).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from django.core.cache import cache
from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from mountain_intelligence.catalog.taxonomy import CATEGORIES

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
from ..discovery import canonicalize_url, discover_from_sitemaps, read_sitemap_locs
from ..http import FetchError
from .base import AdapterContext, AdapterError, register
from .classification import classify
from .jsonld import _types, iter_jsonld_nodes, jsonld_attributes

DEFAULT_PRODUCT_PATTERN = r"^https?://[^/]+/products/[^/.]+$"
_HANDLE_RE = re.compile(r"/products/([^/?#.]+)")
_COLLECTION_RE = re.compile(r"/collections/([^/?#.]+)$")
_COLOR_OPTION = re.compile(r"colou?r|couleur|farbe|colore|kleur", re.IGNORECASE)
_SIZE_OPTION = re.compile(r"size|talla|taille|gr(ö|o)(ß|ss)e|taglia|tamaño|tamano|medida|maat", re.IGNORECASE)
_SHOE_SIZE = re.compile(r"^(\d{2})(?:[.,](\d))?$")
MAX_COLLECTION_PAGES = 20
COLLECTION_PAGE_SIZE = 250


def handle_of(url: str) -> str:
    match = _HANDLE_RE.search(urlsplit(url).path)
    return match.group(1) if match else ""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _https(url: Any) -> str:
    text = str(url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    return text if text.startswith(("http://", "https://")) and len(text) <= 1000 else ""


def description_attributes(html_text: str, limit: int = 60) -> list[SourceAttribute]:
    """``Name: value`` pairs from list items, table rows and short lines of a product description.

    Only short key/value statements are kept (characteristics such as "Peso: 450 g": name up to 6
    words, value up to 120 characters and 15 words); free prose is discarded, so no marketing copy
    is stored.
    """
    if not html_text:
        return []
    tree = LexborHTMLParser(html_text)
    out: list[SourceAttribute] = []
    seen: set[str] = set()

    def add(name: str, value: str, locator: str) -> None:
        name, value = _clean(name, 120).rstrip(":").strip(), _clean(value, 500)
        if not name or not value or len(name) > 60 or len(name.split()) > 6 or name.casefold() in seen:
            return
        if len(value) > 120 or len(value.split()) > 15:
            return
        seen.add(name.casefold())
        try:
            out.append(SourceAttribute(name=name, value=value, locator=locator))
        except ValidationError:
            return

    for i, row in enumerate(tree.css("tr")):
        cells = [c.text(deep=True, strip=True) for c in row.css("th, td")]
        if len(cells) == 2:
            add(cells[0], cells[1], f"shopify:description tr[{i}]")
    for i, node in enumerate(tree.css("li, p")):
        for j, line in enumerate((node.text(deep=True, separator="\n", strip=False) or "").splitlines()):
            if ":" in line:
                key, _, value = line.partition(":")
                add(key, value, f"shopify:description {node.tag}[{i}] line {j}")
        if len(out) >= limit:
            break
    return out[:limit]


class ShopifyProductAdapter:
    key = "shopify_product"
    needs_network = True

    # -- discovery ---------------------------------------------------------------------------
    def _config(self, ctx: AdapterContext) -> dict[str, Any]:
        cfg = dict(ctx.source.config)
        base = (ctx.source.base_url or "").rstrip("/")
        if not cfg.get("sitemap_url") and not cfg.get("sitemap_urls"):
            if not base:
                raise AdapterError("shopify adapter requires config.sitemap_url or the source base_url")
            cfg["sitemap_url"] = f"{base}/sitemap.xml"
        cfg.setdefault("sitemap_include", [r"^https?://[^/]+/sitemap_products_\d+\.xml"])
        cfg.setdefault("product_url_patterns", [DEFAULT_PRODUCT_PATTERN])
        cfg.setdefault("max_urls", 20000)
        cfg.setdefault("max_sitemaps", 50)
        return cfg

    def canonical_key(self, external_id: str, cfg: dict[str, Any]) -> str:
        return canonicalize_url(external_id, cfg) if external_id.startswith(("http://", "https://")) else external_id

    def discover(self, ctx: AdapterContext) -> Iterable[DiscoveredItem]:
        cfg = self._config(ctx)
        result = discover_from_sitemaps(ctx, cfg)
        ctx.record_discovery(result)
        ctx.state["shopify_other_sitemaps"] = result.other_sitemaps
        yield from result.items

    # -- collections (classification signal, cached) -------------------------------------------
    def _collections(self, ctx: AdapterContext) -> dict[str, list[str]]:
        if "shopify_collections" in ctx.state:
            return ctx.state["shopify_collections"]
        settings_ = ctx.source.config.get("collections") or {}
        mapping: dict[str, list[str]] = {}
        ctx.state["shopify_collections"] = mapping
        if not settings_.get("enabled") or ctx.fetcher is None:
            return mapping
        cache_key = f"shopify-collections:{ctx.source.slug}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            mapping.update(cached)
            return mapping
        include = [re.compile(p) for p in settings_.get("include", [])]
        max_collections = min(int(settings_.get("max_collections", 100)), 1000)
        cfg = self._config(ctx)
        handles: list[str] = []
        for sitemap in ctx.state.get("shopify_other_sitemaps", []):
            if "sitemap_collections_" not in sitemap:
                continue
            try:
                locs = read_sitemap_locs(ctx.fetcher, sitemap)
            except FetchError as exc:
                ctx.warnings.append(f"collections sitemap not read ({exc.kind}); classification without collections")
                continue
            for loc in locs:
                match = _COLLECTION_RE.search(urlsplit(canonicalize_url(loc, cfg)).path)
                handle = match.group(1) if match else ""
                if (
                    handle
                    and handle not in handles
                    and handle != "all"
                    and (not include or any(p.search(handle) for p in include))
                ):
                    handles.append(handle)
        if len(handles) > max_collections:
            ctx.warnings.append(
                f"collections limited to {max_collections} of {len(handles)} (classification signal only)"
            )
            handles = handles[:max_collections]
        first_sitemap = cfg.get("sitemap_url") or (cfg.get("sitemap_urls") or [""])[0]
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(canonicalize_url(str(first_sitemap), cfg)))
        complete = True
        for handle in handles:
            for page in range(1, MAX_COLLECTION_PAGES + 1):
                url = f"{origin}/collections/{handle}/products.json?limit={COLLECTION_PAGE_SIZE}&page={page}"
                try:
                    doc = ctx.fetcher.fetch(url, accept="application/json")
                    products = json.loads(doc.body).get("products") or []
                except (FetchError, ValueError, AttributeError):
                    complete = False
                    break
                for product in products:
                    if isinstance(product, dict) and product.get("handle"):
                        mapping.setdefault(str(product["handle"]), []).append(handle)
                if len(products) < COLLECTION_PAGE_SIZE:
                    break
        if not complete:
            ctx.warnings.append("some collection memberships could not be read (classification signal only)")
        else:
            cache.set(cache_key, mapping, timeout=int(settings_.get("cache_hours", 24)) * 3600)
        return mapping

    # -- fetch -------------------------------------------------------------------------------
    def fetch(
        self, item: DiscoveredItem, ctx: AdapterContext, *, etag: str = "", last_modified: str = ""
    ) -> FetchedDocument:
        if ctx.fetcher is None:  # pragma: no cover - pipeline always provides one
            raise AdapterError("network adapter requires a fetcher")
        product_doc = ctx.fetcher.fetch(
            f"{item.url}.js", etag=etag, last_modified=last_modified, accept="application/json", item=item
        )
        if product_doc.not_modified:
            return product_doc
        page_html = ""
        if ctx.source.config.get("jsonld"):
            try:
                page = ctx.fetcher.fetch(item.url, item=item)
                page_html = page.body.decode("utf-8", errors="replace")
            except FetchError as exc:
                ctx.warnings.append(f"product page not read for JSON-LD ({exc.kind})")
        bundle = {
            "product_js": product_doc.body.decode("utf-8", errors="replace"),
            "page_html": page_html,
        }
        return product_doc.model_copy(
            update={"url": item.url, "content_type": "application/json", "body": json.dumps(bundle).encode()}
        )

    # -- extraction --------------------------------------------------------------------------
    def extract(self, document: FetchedDocument, ctx: AdapterContext) -> list[ExtractedProduct]:
        try:
            bundle = json.loads(document.body)
            product = json.loads(bundle["product_js"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError("storefront product JSON is not valid") from exc
        if not isinstance(product, dict) or not product.get("title"):
            raise AdapterError("storefront product JSON has no title")
        jsonld = self._jsonld_product(bundle.get("page_html") or "")
        return [self._to_contract(product, jsonld, document, ctx)]

    @staticmethod
    def _jsonld_product(page_html: str) -> dict[str, Any] | None:
        if not page_html:
            return None
        nodes = list(iter_jsonld_nodes(page_html))
        groups = [n for n in nodes if "ProductGroup" in _types(n)]
        products = [n for n in nodes if "Product" in _types(n)]
        return groups[0] if groups else (products[0] if products else None)

    def _to_contract(
        self, product: dict[str, Any], jsonld: dict[str, Any] | None, doc: FetchedDocument, ctx: AdapterContext
    ) -> ExtractedProduct:
        cfg = self._config(ctx)
        url = canonicalize_url(doc.item.url or doc.url, cfg)
        handle = str(product.get("handle") or handle_of(url))
        name = _clean(product.get("title"), 200)
        product_type = _clean(product.get("type") or product.get("product_type"), 120)
        tags = [_clean(t, 80) for t in (product.get("tags") or [])[:100] if _clean(t, 80)]
        collections = self._collections(ctx).get(handle, [])[:50]
        jsonld_category = _clean((jsonld or {}).get("category"), 300) if jsonld else ""
        source_category = product_type or jsonld_category
        classification = classify(
            cfg,
            url=url,
            name=name,
            product_type=product_type,
            tags=tags,
            source_category=" ".join([source_category, *(c.replace("-", " ") for c in collections)]),
        )
        definition = CATEGORIES.get(classification.category)
        footwear = bool(definition and definition.family == "footwear")

        # Options: which position holds colour / size.
        options = [o for o in product.get("options") or [] if isinstance(o, dict)]
        option_names = [_clean(o.get("name"), 40) or f"option{i + 1}" for i, o in enumerate(options[:3])]
        color_idx = next((i for i, n in enumerate(option_names) if _COLOR_OPTION.search(n)), None)
        size_idx = next((i for i, n in enumerate(option_names) if _SIZE_OPTION.search(n)), None)

        variants: list[ExtractedVariant] = []
        offers: list[ExtractedOffer] = []
        image_skus: dict[str, list[str]] = {}
        currency = str(cfg.get("currency", "")).upper()
        if cfg.get("capture_offers") and not re.fullmatch(r"[A-Z]{3}", currency):
            ctx.warnings.append("capture_offers requires config.currency (ISO 4217): prices not captured")
        for raw in (product.get("variants") or [])[:500]:
            if not isinstance(raw, dict):
                continue
            values = [_clean(raw.get(f"option{i}"), 80) for i in (1, 2, 3)]
            opts = {
                option_names[i]: values[i]
                for i in range(min(len(option_names), 3))
                if values[i] and values[i] not in {"-", "Default Title"}
            }
            color = values[color_idx] if color_idx is not None and values[color_idx] != "-" else ""
            size = values[size_idx] if size_idx is not None and values[size_idx] != "-" else ""
            size_eu = None
            if footwear and (m := _SHOE_SIZE.match(size)):
                size_eu = Decimal(f"{m.group(1)}.{m.group(2) or 0}")
            barcode = re.sub(r"\D", "", str(raw.get("barcode") or ""))
            sku = _clean(raw.get("sku"), 80)
            image = _https(
                (raw.get("featured_image") or {}).get("src") if isinstance(raw.get("featured_image"), dict) else ""
            )
            try:
                variants.append(
                    ExtractedVariant(
                        external_id=_clean(raw.get("id"), 100),
                        sku=sku,
                        gtin=barcode if barcode and gtin_is_valid(barcode) else "",
                        size_label=size[:40],
                        size_eu=size_eu if size_eu is not None and 15 <= size_eu <= 55 else None,
                        color=color[:80],
                        options=opts,
                        image_url=image,
                    )
                )
            except ValidationError:
                ctx.warnings.append("skipped an invalid variant")
                continue
            if image and sku:
                image_skus.setdefault(image, []).append(sku)
            if cfg.get("capture_offers") and re.fullmatch(r"[A-Z]{3}", currency):
                offer = self._offer(raw, url, currency, sku, ctx)
                if offer is not None:
                    offers.append(offer)

        images = self._images(product, image_skus)
        attributes: list[SourceAttribute] = []
        if jsonld:
            attributes.extend(jsonld_attributes(jsonld))
        if cfg.get("description_attributes"):
            attributes.extend(description_attributes(str(product.get("description") or "")))
        if product.get("vendor"):
            attributes.append(
                SourceAttribute(name="vendor", value=_clean(product["vendor"], 500), locator="shopify:$.vendor")
            )
        specs, locators = self._mapped_specs(attributes, cfg)

        if cfg.get("brand_from", "source" if ctx.source.brand else "vendor") == "vendor":
            brand = _clean(product.get("vendor"), 120)
        else:
            brand = ctx.source.brand.name if ctx.source.brand else ""
        external_ids = {
            k: v
            for k, v in (("shopify_product_id", _clean(product.get("id"), 300)), ("shopify_handle", handle[:300]))
            if v
        }
        try:
            return ExtractedProduct(
                external_id=url[:300],
                canonical_url=url[:1000],
                brand=brand or "unknown",
                brand_domain=(ctx.source.brand.official_domain if ctx.source.brand else ""),
                name=name,
                category=classification.category,
                category_basis=classification.basis,  # type: ignore[arg-type]
                specifications=specs,
                locators=locators,
                variants=variants,
                offers=offers[:500],
                source_category=source_category,
                product_type=product_type,
                tags=tags,
                collections=collections,
                external_ids=external_ids,
                images=images,
                source_attributes=_dedupe_attributes(attributes)[:150],
            )
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise AdapterError(f"incomplete product metadata: {', '.join(fields)}") from exc

    @staticmethod
    def _offer(raw: dict[str, Any], url: str, currency: str, sku: str, ctx: AdapterContext) -> ExtractedOffer | None:
        price = raw.get("price")
        try:
            # Storefront .js prices are integer minor units; .json prices are decimal strings.
            amount = Decimal(price) / 100 if isinstance(price, int) else Decimal(str(price))
            availability = "in_stock" if raw.get("available") is True else "out_of_stock"
            if raw.get("available") is None:
                availability = "unknown"
            variant_id = _clean(raw.get("id"), 40)
            return ExtractedOffer(
                retailer=ctx.source.name[:120],
                url=(f"{url}?variant={variant_id}" if variant_id else url)[:1000],
                currency=currency,
                price=amount,
                availability=availability,  # type: ignore[arg-type]
                variant_sku=sku,
            )
        except (InvalidOperation, ValidationError, TypeError, ValueError):
            ctx.warnings.append("skipped an invalid variant price")
            return None

    @staticmethod
    def _images(product: dict[str, Any], image_skus: dict[str, list[str]]) -> list[ExtractedImage]:
        media = [
            m for m in product.get("media") or [] if isinstance(m, dict) and m.get("media_type", "image") == "image"
        ]
        raw_items: list[tuple[str, str]] = [(_https(m.get("src")), _clean(m.get("alt"), 200)) for m in media]
        if not raw_items:
            raw_items = [(_https(src), "") for src in product.get("images") or [] if isinstance(src, str)]
        out: list[ExtractedImage] = []
        seen: set[str] = set()
        for position, (url, alt) in enumerate(raw_items, start=1):
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                out.append(
                    ExtractedImage(url=url, alt=alt, position=position, variant_skus=image_skus.get(url, [])[:100])
                )
            except ValidationError:
                continue
            if len(out) >= 50:
                break
        return out

    @staticmethod
    def _mapped_specs(
        attributes: list[SourceAttribute], cfg: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, FieldLocator]]:
        property_map = {k.casefold(): v for k, v in (cfg.get("property_map") or {}).items()}
        specs: dict[str, Any] = {}
        locators: dict[str, FieldLocator] = {}
        for attr in attributes:
            mapping = property_map.get(attr.name.casefold())
            if not mapping or mapping["field"] in specs:
                continue
            specs[mapping["field"]] = attr.value
            locators[mapping["field"]] = FieldLocator(
                locator=attr.locator,
                unit=attr.unit or mapping.get("unit"),
                excerpt=f"{attr.name}: {attr.value} {attr.unit}".strip()[:280],
            )
        return specs, locators


def _dedupe_attributes(attributes: list[SourceAttribute]) -> list[SourceAttribute]:
    seen: set[tuple[str, str]] = set()
    out = []
    for attr in attributes:
        key = (attr.name.casefold(), attr.value.casefold())
        if key not in seen:
            seen.add(key)
            out.append(attr)
    return out


register(ShopifyProductAdapter())


def extract_from_storefront_json(
    product_js: str, ctx: AdapterContext, url: str, page_html: str = ""
) -> ExtractedProduct:
    """Convenience for tests and operator dry-runs on saved storefront JSON."""
    doc = FetchedDocument(
        item=DiscoveredItem(external_id=url, url=url),
        url=url,
        content_type="application/json",
        body=json.dumps({"product_js": product_js, "page_html": page_html}).encode(),
        fetched_at=datetime.now(UTC),
    )
    return ShopifyProductAdapter().extract(doc, ctx)[0]
