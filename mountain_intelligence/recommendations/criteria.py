"""Tri-state evaluation of a criterion against a product: True, False or None (unknown)."""

from __future__ import annotations

import re
from typing import Any

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.catalog.normalization import fold, normalize_value
from mountain_intelligence.catalog.queries import QueryValidationError
from mountain_intelligence.catalog.services import spec_value
from mountain_intelligence.catalog.taxonomy import CategoryDefinition, FieldType
from mountain_intelligence.catalog.units import UnitError


def normalize_criterion_value(definition: CategoryDefinition, field: str, op: str, value: Any) -> Any:
    spec = definition.field_map.get(field)
    if spec is None or not spec.filterable:
        allowed = [f.name for f in definition.fields if f.filterable]
        raise QueryValidationError(f"field {field!r} not usable for {definition.slug!r}; allowed: {allowed}")
    if op not in spec.operators:
        raise QueryValidationError(f"operator {op!r} not supported for {field!r}; allowed: {list(spec.operators)}")
    try:
        if op == "in":
            if not isinstance(value, list) or not value:
                raise QueryValidationError("'in' expects a non-empty list")
            return [normalize_value(spec, v, assumed_unit=spec.unit).normalized for v in value]
        if spec.type in (FieldType.ENUM_LIST, FieldType.TEXT_LIST):
            items = value if isinstance(value, list) else [value]
            return normalize_value(spec, items).normalized
        return normalize_value(spec, value, assumed_unit=spec.unit).normalized
    except (UnitError, ValueError, TypeError) as exc:
        if isinstance(exc, QueryValidationError):
            raise
        raise QueryValidationError(f"invalid value for {field!r}: {exc}") from exc


_YEAR_SUFFIX = re.compile(r"[:/]\s*\d{4}.*$")


def canonical_token(value: Any) -> Any:
    """Tolerant comparison key for list items: 'EN 12492:2012' == 'en12492' == 'EN-12492'."""
    if not isinstance(value, str):
        return value
    return re.sub(r"[^a-z0-9]", "", fold(_YEAR_SUFFIX.sub("", value)))


def evaluate(product: Product, field: str, op: str, expected: Any) -> bool | None:
    actual = spec_value(product, field)
    if actual is None:
        return None
    if op == "eq":
        return actual == expected
    if op == "in":
        return actual in expected
    if op == "gte":
        return float(actual) >= float(expected)
    if op == "lte":
        return float(actual) <= float(expected)
    have = {canonical_token(v) for v in actual}
    wanted = [canonical_token(v) for v in expected]
    if op in ("contains", "contains_all"):
        return all(v in have for v in wanted)
    if op == "contains_any":
        return any(v in have for v in wanted)
    raise QueryValidationError(f"unsupported operator {op!r}")  # pragma: no cover


def describe(field: str, op: str, value: Any) -> str:
    symbols = {
        "eq": "=",
        "in": "in",
        "gte": ">=",
        "lte": "<=",
        "contains": "contains",
        "contains_all": "contains all of",
        "contains_any": "contains any of",
    }
    return f"{field} {symbols.get(op, op)} {value}"
