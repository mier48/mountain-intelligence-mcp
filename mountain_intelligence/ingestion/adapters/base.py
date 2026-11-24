"""Adapter contract and registry.

An adapter discovers items for a source, fetches each one and extracts `ExtractedProduct`
contracts. It never writes to the database: normalization, matching, review and persistence
live in `ingestion.pipeline` and are shared by every adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..contracts import DiscoveredItem, ExtractedProduct, FetchedDocument

if TYPE_CHECKING:
    from mountain_intelligence.sources.models import Source

    from ..http import SafeFetcher


class AdapterError(Exception):
    """An item-level extraction problem (the run continues with other items)."""


@dataclass
class AdapterContext:
    source: Source
    fetcher: SafeFetcher | None = None
    max_items: int | None = None
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(Protocol):
    key: str
    needs_network: bool

    def discover(self, ctx: AdapterContext) -> Iterable[DiscoveredItem]: ...

    def fetch(
        self, item: DiscoveredItem, ctx: AdapterContext, *, etag: str = "", last_modified: str = ""
    ) -> FetchedDocument: ...

    def extract(self, document: FetchedDocument, ctx: AdapterContext) -> list[ExtractedProduct]: ...


_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    _REGISTRY[adapter.key] = adapter
    return adapter


def get_adapter(key: str) -> SourceAdapter:
    from . import jsonld, structured  # noqa: F401  (registration side effects)

    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"unknown adapter {key!r}; available: {sorted(_REGISTRY)}") from exc


def available_adapters() -> list[str]:
    from . import jsonld, structured  # noqa: F401

    return sorted(_REGISTRY)


def limited(items: Iterable[DiscoveredItem], limit: int | None) -> Iterator[DiscoveredItem]:
    for n, item in enumerate(items):
        if limit is not None and n >= limit:
            return
        yield item
