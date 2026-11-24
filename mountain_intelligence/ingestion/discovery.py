"""Sitemap discovery shared by the website adapters (JSON-LD, Shopify).

Discovery finds *every* product page a manufacturer publishes in its sitemaps. It does not filter
by category keywords or commercial names: scoping by category happens after extraction (see
``Source.categories``). It only applies structural rules:

- ``sitemap_include``: regexes on child sitemap URLs of a sitemap index (e.g. only the product
  sitemaps of a Shopify store). Children that do not match are not fetched.
- ``product_url_patterns``: regexes a URL must match to be a product page (structural, e.g.
  ``^https://shop\\.test/products/[^/]+$``). ``url_include`` (legacy) behaves the same way.
- ``url_exclude``: regexes of URLs to drop (e.g. gift cards).

Every URL is canonicalized before de-duplication (see :func:`canonicalize_url`), so language copies
(``/en/products/x``), collection-scoped paths (``/collections/c/products/x``), query strings
(``?variant=1``) and trailing slashes collapse into one item.

Completeness is explicit: the result says whether discovery covered the whole catalog. It is
``False`` when a child sitemap could not be read, the sitemap document budget ran out or the URL
safety cap was hit. An incomplete discovery is never used to flag products as missing.
"""

from __future__ import annotations

import html
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import DiscoveredItem
from .http import FetchError

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # sitemap protocol limit (uncompressed)
DEFAULT_MAX_SITEMAP_DOCS = 20
HARD_MAX_SITEMAP_DOCS = 500
DEFAULT_MAX_URLS = 200
HARD_MAX_URLS = 50_000  # sitemap protocol: 50,000 URLs per file

_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.IGNORECASE | re.DOTALL)
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap>(.*?)</sitemap>", re.IGNORECASE | re.DOTALL)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)
# /en/products/x, /es-es/products/x, /en-gb/collections/c/products/x ...
DEFAULT_LOCALE_PREFIX = r"^/[a-z]{2}(?:[-_][a-z]{2})?(?=/(?:products|collections)/)"
_COLLECTION_PRODUCT_RE = re.compile(r"^/collections/[^/]+(/products/[^/]+)$")


@dataclass
class DiscoveryResult:
    items: list[DiscoveredItem] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    sitemaps_read: int = 0
    duplicates_collapsed: int = 0
    # Child sitemaps of an index that did not match `sitemap_include` (e.g. Shopify collections).
    other_sitemaps: list[str] = field(default_factory=list)

    def truncate(self, note: str) -> None:
        self.complete = False
        self.notes.append(note)


def canonicalize_url(url: str, cfg: dict[str, Any] | None = None) -> str:
    """Stable product URL used as identity: one item per product, whatever the locale or path.

    - scheme and host lower-cased (``canonical_host`` can force one host, e.g. drop ``www.``);
    - query string and fragment removed (``?variant=``, tracking parameters);
    - locale prefix removed (``/en/products/x`` -> ``/products/x``); disable with
      ``strip_locale_prefix: false`` or customize with ``locale_prefix_pattern``;
    - collection-scoped product paths collapsed (``/collections/c/products/x`` -> ``/products/x``);
    - trailing slash removed.
    """
    cfg = cfg or {}
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (cfg.get("canonical_host") or parts.netloc).lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if cfg.get("strip_locale_prefix", True):
        pattern = cfg.get("locale_prefix_pattern") or DEFAULT_LOCALE_PREFIX
        path = re.sub(pattern, "", path, count=1, flags=re.IGNORECASE)
    if cfg.get("collapse_collection_paths", True):
        match = _COLLECTION_PRODUCT_RE.match(path)
        if match:
            path = match.group(1)
    if len(path) > 1:
        path = path.rstrip("/")
    if cfg.get("lowercase_path", False):
        path = path.lower()
    return urlunsplit((scheme, host, path, "", ""))


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


def _read_sitemap(fetcher, url: str) -> str:
    doc = fetcher.fetch(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.5", max_bytes=MAX_SITEMAP_BYTES)
    body = doc.body
    if urlsplit(url).path.endswith(".gz") or body[:2] == b"\x1f\x8b":
        # Bounded decompression: protects against gzip bombs.
        body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, MAX_SITEMAP_BYTES)
    return body.decode("utf-8", errors="replace")


def read_sitemap_locs(fetcher, url: str) -> list[str]:
    """All <loc> values of one urlset sitemap (used for auxiliary sitemaps such as collections)."""
    text = _read_sitemap(fetcher, url)
    return [html.unescape(m.group(1)) for b in _URL_BLOCK_RE.findall(text) if (m := _LOC_RE.search(b))]


def discover_from_sitemaps(ctx, cfg: dict[str, Any]) -> DiscoveryResult:
    """Read the sitemap (index) in ``cfg['sitemap_url']`` and return canonical, de-duplicated items.

    The root sitemap must be readable (a failure raises ``FetchError``: the run fails). Child
    sitemap failures only make the result incomplete.
    """
    assert ctx.fetcher is not None
    roots = cfg.get("sitemap_urls") or [cfg["sitemap_url"]]
    sitemap_include = _compile(cfg.get("sitemap_include", []))
    product_patterns = _compile([*cfg.get("product_url_patterns", []), *cfg.get("url_include", [])])
    exclude = _compile(cfg.get("url_exclude", []))
    max_docs = min(int(cfg.get("max_sitemaps", DEFAULT_MAX_SITEMAP_DOCS)), HARD_MAX_SITEMAP_DOCS)
    max_urls = min(int(cfg.get("max_urls", DEFAULT_MAX_URLS)), HARD_MAX_URLS)

    result = DiscoveryResult()
    by_key: dict[str, DiscoveredItem] = {}
    pending: list[tuple[str, bool]] = [(str(r), True) for r in roots]
    seen_sitemaps: set[str] = set()
    while pending:
        url, is_root = pending.pop(0)
        if url in seen_sitemaps:
            continue
        if result.sitemaps_read >= max_docs:
            result.truncate(f"sitemap budget exhausted (max_sitemaps={max_docs}); {len(pending) + 1} not read")
            break
        seen_sitemaps.add(url)
        try:
            text = _read_sitemap(ctx.fetcher, url)
        except FetchError as exc:
            if is_root:
                raise
            result.truncate(f"child sitemap not read ({exc.kind}): {url[:200]}")
            continue
        except zlib.error:
            result.truncate(f"child sitemap is not valid gzip: {url[:200]}")
            continue
        result.sitemaps_read += 1
        if "<sitemapindex" in text[:4000].lower():
            for block in _SITEMAP_BLOCK_RE.findall(text):
                loc = _LOC_RE.search(block)
                if not loc:
                    continue
                child = html.unescape(loc.group(1))
                if sitemap_include and not any(p.search(child) for p in sitemap_include):
                    result.other_sitemaps.append(child)
                    continue
                pending.append((child, False))
            continue
        for block in _URL_BLOCK_RE.findall(text):
            loc = _LOC_RE.search(block)
            if not loc:
                continue
            raw = html.unescape(loc.group(1))
            canonical = canonicalize_url(raw, cfg)
            if product_patterns and not any(p.search(canonical) for p in product_patterns):
                continue
            if any(p.search(canonical) for p in exclude):
                continue
            if canonical in by_key:
                result.duplicates_collapsed += 1
                continue
            if len(by_key) >= max_urls:
                result.truncate(f"discovery capped at max_urls={max_urls}")
                break
            lastmod = _LASTMOD_RE.search(block)
            by_key[canonical] = DiscoveredItem(
                external_id=canonical, url=canonical, lastmod=(lastmod.group(1)[:40] if lastmod else "")
            )
        if not result.complete and any("max_urls" in n for n in result.notes):
            break
    # Deterministic order: batches and cursors are stable between runs.
    result.items = [by_key[k] for k in sorted(by_key)]
    if result.duplicates_collapsed:
        result.notes.append(f"{result.duplicates_collapsed} duplicate URLs collapsed (locales/paths/parameters)")
    return result
