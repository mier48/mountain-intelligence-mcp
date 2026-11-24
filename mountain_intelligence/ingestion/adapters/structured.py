"""Structured JSON/CSV import adapter for authorized datasets delivered to the operator.

JSON format::

    {"format": "mountain-intelligence/products", "version": 1, "products": [ {...ExtractedProduct...} ]}

CSV format: one product per row; columns ``external_id, brand, name, category`` (required),
optional ``generation, mpn, canonical_url, model_year, aliases, summary``, and one
``spec.<field>`` column per specification (list values separated by ``;``). Units are given by
the field name contract (``spec.weight_g`` is grams).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from pydantic import ValidationError

from ..contracts import DiscoveredItem, ExtractedProduct, FetchedDocument
from .base import AdapterContext, AdapterError, register

FORMAT_ID = "mountain-intelligence/products"
MAX_FILE_BYTES = 50 * 1024 * 1024


def allowed_import_roots() -> list[Path]:
    roots = [settings.DATA_DIR, settings.FIXTURES_DIR]
    extra = getattr(settings, "MI_IMPORT_DIR", None)
    if extra:
        roots.append(Path(extra))
    return [Path(r).resolve() for r in roots]


def resolve_source_path(relative: str) -> Path:
    """Resolve a path from source config, confined to the configured import roots."""
    for root in allowed_import_roots():
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    raise AdapterError(f"file {relative!r} not found inside the allowed import directories")


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise AdapterError("import file too large")
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        return list(_csv_records(raw))
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("format") != FORMAT_ID or data.get("version") != 1:
        raise AdapterError(f"JSON import must declare format={FORMAT_ID!r} and version=1")
    products = data.get("products")
    if not isinstance(products, list):
        raise AdapterError("'products' must be a list")
    return products


def _csv_records(raw: str) -> Iterable[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        record: dict[str, Any] = {"specifications": {}}
        for column, raw_value in row.items():
            if column is None:
                continue
            value = (raw_value or "").strip()
            if column.startswith("spec."):
                if value:
                    record["specifications"][column[5:]] = value
            elif column == "aliases":
                record["aliases"] = [a.strip() for a in value.split(";") if a.strip()]
            elif column == "model_year":
                record["model_year"] = int(value) if value else None
            elif value:
                record[column] = value
        yield record


def parse_record(record: dict[str, Any]) -> ExtractedProduct:
    payload = dict(record)
    payload["units_from_field_names"] = True
    try:
        return ExtractedProduct.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise AdapterError(f"invalid record: {', '.join(fields)}") from exc


class StructuredFileAdapter:
    key = "structured_file"
    needs_network = False

    def discover(self, ctx: AdapterContext) -> Iterable[DiscoveredItem]:
        path_value = ctx.source.config.get("path")
        if not path_value:
            raise AdapterError("structured_file source requires config.path")
        path = resolve_source_path(str(path_value))
        for index, record in enumerate(load_records(path)):
            external_id = str(record.get("external_id") or f"row-{index + 1}")
            yield DiscoveredItem(
                external_id=external_id, url=str(record.get("canonical_url") or ""), inline_payload=record
            )

    def fetch(
        self, item: DiscoveredItem, ctx: AdapterContext, *, etag: str = "", last_modified: str = ""
    ) -> FetchedDocument:
        return FetchedDocument(
            item=item,
            url=item.url,
            body=json.dumps(item.inline_payload).encode(),
            content_type="application/json",
            fetched_at=datetime.now(UTC),
        )

    def extract(self, document: FetchedDocument, ctx: AdapterContext) -> list[ExtractedProduct]:
        return [parse_record(document.item.inline_payload or {})]


register(StructuredFileAdapter())
