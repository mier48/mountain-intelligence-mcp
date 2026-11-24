"""Normalization and validation of raw specification values against a category schema.

Every write path (demo seed, structured import, adapters, review approval) goes through
`normalize_specifications`, which returns normalized values plus a per-field record of the
original value, and a list of issues. Invalid or ambiguous values are never stored as
normalized facts: they are reported so the caller can block them or open a review item.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .taxonomy import CategoryDefinition, FieldSpec, FieldType
from .units import UnitError, parse_number, to_canonical

_TRUE = {"true", "yes", "y", "si", "sí", "1", "x", "✓", "included", "incluido"}
_FALSE = {"false", "no", "n", "0", "none", "ninguno", "not included", "no incluido"}


@dataclass
class FieldValue:
    field: str
    original: Any
    normalized: Any
    unit_original: str | None = None
    unit: str | None = None
    note: str | None = None


@dataclass
class FieldIssue:
    field: str
    original: Any
    code: str  # unknown_field | invalid_value | ambiguous_unit | inconsistent
    message: str


@dataclass
class NormalizationResult:
    values: dict[str, FieldValue] = field(default_factory=dict)
    issues: list[FieldIssue] = field(default_factory=list)

    @property
    def specifications(self) -> dict[str, Any]:
        return {k: v.normalized for k, v in self.values.items()}


def fold(text: str) -> str:
    """Lower-case and strip accents (used for tolerant matching of enum labels and search)."""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _as_list(raw: Any) -> list[Any]:
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    if isinstance(raw, str):
        sep = ";" if ";" in raw else ","
        return [part.strip() for part in raw.split(sep) if part.strip()]
    return [raw]


def _enum_value(spec: FieldSpec, raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"expected text, got {type(raw).__name__}")
    folded = fold(raw)
    for choice in spec.choices:
        if folded == fold(choice) or folded == fold(choice.replace("_", " ")):
            return choice
    aliases = {fold(k): v for k, v in spec.choice_aliases.items()}
    if folded in aliases:
        return aliases[folded]
    raise ValueError(f"{raw!r} is not one of {list(spec.choices)}")


def normalize_value(
    spec: FieldSpec, raw: Any, *, assumed_unit: str | None = None, explicit_unit: str | None = None
) -> FieldValue:
    """Normalize one raw value. Raises ValueError/UnitError on invalid or ambiguous input."""
    if spec.type in (FieldType.NUMBER, FieldType.INTEGER):
        if spec.quantity:
            parsed = to_canonical(
                raw,
                spec.quantity,
                assumed_unit=assumed_unit,
                explicit_unit=explicit_unit,
                minimum=spec.minimum,
                maximum=spec.maximum,
            )
            value: Any = parsed.value
            unit_original, note = parsed.unit_original, parsed.note
        else:
            if isinstance(raw, bool):
                raise ValueError("boolean is not a number")
            if isinstance(raw, str):
                candidates = [float(c) for c in parse_number(raw)]
                in_range = [
                    c
                    for c in candidates
                    if (spec.minimum is None or c >= spec.minimum) and (spec.maximum is None or c <= spec.maximum)
                ]
                if len(candidates) > 1 and len(in_range) != 1:
                    raise UnitError(f"ambiguous number format {raw!r}")
                value = in_range[0] if in_range else candidates[0]
            else:
                value = float(raw)
            unit_original, note = None, None
        if spec.type is FieldType.INTEGER:
            if float(value) != int(value):
                raise ValueError(f"{raw!r} is not an integer")
            value = int(value)
        return FieldValue(spec.name, raw, value, unit_original, spec.unit, note)
    if spec.type is FieldType.BOOLEAN:
        if isinstance(raw, bool):
            return FieldValue(spec.name, raw, raw)
        folded = fold(str(raw))
        if folded in {fold(t) for t in _TRUE}:
            return FieldValue(spec.name, raw, True)
        if folded in {fold(f) for f in _FALSE}:
            return FieldValue(spec.name, raw, False)
        raise ValueError(f"{raw!r} is not a recognised boolean")
    if spec.type is FieldType.ENUM:
        return FieldValue(spec.name, raw, _enum_value(spec, raw))
    if spec.type is FieldType.ENUM_LIST:
        return FieldValue(spec.name, raw, list(dict.fromkeys(_enum_value(spec, item) for item in _as_list(raw))))
    if spec.type is FieldType.TEXT:
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            raise ValueError("expected text")
        text = " ".join(str(raw).split())
        if not text or len(text) > spec.max_length:
            raise ValueError(f"text length must be 1..{spec.max_length}")
        return FieldValue(spec.name, raw, text)
    items = [" ".join(str(item).split()) for item in _as_list(raw) if str(item).strip()]
    if any(len(i) > spec.max_length for i in items):
        raise ValueError("list item too long")
    return FieldValue(spec.name, raw, list(dict.fromkeys(items)))


def normalize_specifications(
    category: CategoryDefinition,
    raw_specs: dict[str, Any],
    *,
    units_from_field_names: bool = False,
    explicit_units: dict[str, str] | None = None,
) -> NormalizationResult:
    """Normalize a raw specification mapping for `category`.

    `units_from_field_names=True` is used by structured imports whose contract names the unit in
    the key (``weight_g``, ``capacity_l``): bare numbers are then taken in that unit. Adapters for
    free-form sources must pass explicit units instead, otherwise bare numbers are ambiguous.
    `None` values mean "unknown" and are skipped (never converted to false/zero).
    """
    result = NormalizationResult()
    explicit_units = explicit_units or {}
    for name, raw in raw_specs.items():
        spec = category.field_map.get(name)
        if spec is None or spec.derived:
            result.issues.append(
                FieldIssue(name, raw, "unknown_field", f"field {name!r} is not part of the {category.slug} schema")
            )
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            value = normalize_value(
                spec,
                raw,
                assumed_unit=spec.unit if units_from_field_names and spec.quantity else None,
                explicit_unit=explicit_units.get(name),
            )
        except UnitError as exc:
            result.issues.append(FieldIssue(name, raw, "ambiguous_unit", str(exc)))
            continue
        except (ValueError, TypeError) as exc:
            result.issues.append(FieldIssue(name, raw, "invalid_value", str(exc)))
            continue
        result.values[name] = value

    # Schema validation (types, enums, ranges). Offending fields are removed and reported.
    while True:
        try:
            category.model.model_validate(result.specifications)
            break
        except ValidationError as exc:
            bad = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
            if not bad:  # pragma: no cover - defensive
                raise
            for name in bad:
                fv = result.values.pop(name, None)
                messages = "; ".join(e["msg"] for e in exc.errors() if e["loc"] and str(e["loc"][0]) == name)
                result.issues.append(FieldIssue(name, fv.original if fv else None, "invalid_value", messages))

    for message in category.check_consistency(result.specifications):
        result.issues.append(FieldIssue("*", None, "inconsistent", message))
    return result
