"""Parsing of raw numeric values and units into canonical units.

Canonical units: grams (g), millimetres (mm), litres (l), degrees Celsius (c), EUR for money.
Original values are always preserved by callers; this module only converts and reports
ambiguity so that ambiguous input is sent to review instead of being guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# quantity -> {unit alias -> factor to canonical} (temperature handled separately)
_UNIT_FACTORS: dict[str, dict[str, Decimal]] = {
    "mass": {
        "g": Decimal(1),
        "gr": Decimal(1),
        "grs": Decimal(1),
        "gram": Decimal(1),
        "grams": Decimal(1),
        "gramos": Decimal(1),
        "kg": Decimal(1000),
        "kgs": Decimal(1000),
        "kilogram": Decimal(1000),
        "kilograms": Decimal(1000),
        "kilogramos": Decimal(1000),
        "oz": Decimal("28.349523125"),
        "ounce": Decimal("28.349523125"),
        "ounces": Decimal("28.349523125"),
        "lb": Decimal("453.59237"),
        "lbs": Decimal("453.59237"),
        "pound": Decimal("453.59237"),
        "pounds": Decimal("453.59237"),
    },
    "length": {
        "mm": Decimal(1),
        "millimeter": Decimal(1),
        "millimetre": Decimal(1),
        "milimetros": Decimal(1),
        "cm": Decimal(10),
        "centimeter": Decimal(10),
        "centimetre": Decimal(10),
        "centimetros": Decimal(10),
        "m": Decimal(1000),
        "meter": Decimal(1000),
        "metre": Decimal(1000),
        "metros": Decimal(1000),
        "in": Decimal("25.4"),
        "inch": Decimal("25.4"),
        "inches": Decimal("25.4"),
        '"': Decimal("25.4"),
    },
    "volume": {
        "l": Decimal(1),
        "lt": Decimal(1),
        "ltr": Decimal(1),
        "liter": Decimal(1),
        "liters": Decimal(1),
        "litre": Decimal(1),
        "litres": Decimal(1),
        "litro": Decimal(1),
        "litros": Decimal(1),
        "ml": Decimal("0.001"),
        "cl": Decimal("0.01"),
    },
    # Hydrostatic head is expressed in mm of water column; "mm" only.
    "water_column": {"mm": Decimal(1), "mmh2o": Decimal(1)},
    "money": {"eur": Decimal(1), "€": Decimal(1)},
}
_CANONICAL = {"mass": "g", "length": "mm", "volume": "l", "water_column": "mm", "money": "eur", "temperature": "c"}

# UN/CEFACT common codes used by schema.org QuantitativeValue.unitCode
UNIT_CODES = {
    "GRM": "g",
    "KGM": "kg",
    "ONZ": "oz",
    "LBR": "lb",
    "MMT": "mm",
    "CMT": "cm",
    "MTR": "m",
    "INH": "in",
    "LTR": "l",
    "MLT": "ml",
    "CEL": "c",
    "FAH": "f",
}

_NUMBER_UNIT_RE = re.compile(r"^\s*(?P<num>[-+]?\d[\d.,\s]*\d|[-+]?\d)\s*(?P<unit>[a-zA-Z€\"°ºh2O]*\.?)\s*$")


class UnitError(ValueError):
    """Raised when a value cannot be converted unambiguously."""


@dataclass(frozen=True)
class ParsedQuantity:
    value: float
    unit_original: str | None
    canonical_unit: str | None
    note: str | None = None


def parse_number(text: str) -> list[Decimal]:
    """Return every plausible interpretation of a localized number string.

    "1.250" and "1,250" are ambiguous (decimal comma/point vs thousands separator) and yield
    two candidates; callers must disambiguate with the field's plausible range or reject.
    """
    s = text.strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        raise UnitError("empty number")
    sign = ""
    if s[0] in "+-":
        sign, s = s[0], s[1:]
    if not re.fullmatch(r"[\d.,]+", s):
        raise UnitError(f"not a number: {text!r}")
    candidates: list[str] = []
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # The last separator is the decimal separator.
        if s.rfind(".") > s.rfind(","):
            candidates.append(s.replace(",", ""))
        else:
            candidates.append(s.replace(".", "").replace(",", "."))
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = s.split(sep)
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                candidates.append("".join(parts))  # thousands separators only
            else:
                raise UnitError(f"malformed number: {text!r}")
        elif len(parts[1]) == 3:
            candidates.extend([parts[0] + parts[1], parts[0] + "." + parts[1]])  # ambiguous
        else:
            candidates.append(parts[0] + "." + parts[1])
    else:
        candidates.append(s)
    try:
        return [Decimal(sign + c) for c in dict.fromkeys(candidates)]
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise UnitError(f"not a number: {text!r}") from exc


def normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    u = unit.strip().lower().rstrip(".").replace("°", "").replace("º", "")
    if not u:
        return None
    return UNIT_CODES.get(unit.strip().upper(), u)


def to_canonical(
    raw: object,
    quantity: str,
    *,
    assumed_unit: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    explicit_unit: str | None = None,
) -> ParsedQuantity:
    """Convert a raw value (number or string with optional unit) into the canonical unit.

    - `explicit_unit` comes from structured metadata (e.g. schema.org unitCode/unitText).
    - `assumed_unit` is used only when the input carries no unit and the caller knows the unit
      from a trustworthy contract (e.g. a structured import column named ``weight_g``).
    - Without any unit the value is rejected as ambiguous.
    """
    if isinstance(raw, bool):
        raise UnitError("boolean is not a quantity")
    unit_text: str | None = None
    if isinstance(raw, (int, float, Decimal)):
        candidates = [Decimal(str(raw))]
    elif isinstance(raw, str):
        match = _NUMBER_UNIT_RE.match(raw.replace("\u00a0", " "))
        if not match:
            raise UnitError(f"cannot parse quantity {raw!r}")
        candidates = parse_number(match.group("num"))
        unit_text = match.group("unit") or None
    else:
        raise UnitError(f"unsupported value type {type(raw).__name__}")

    unit = normalize_unit(unit_text) or normalize_unit(explicit_unit) or normalize_unit(assumed_unit)
    if unit is None:
        raise UnitError("missing unit")

    if quantity == "temperature":
        if unit not in {"c", "f", "celsius", "fahrenheit"}:
            raise UnitError(f"unknown temperature unit {unit!r}")
        convert = (lambda v: (v - 32) * Decimal(5) / Decimal(9)) if unit in {"f", "fahrenheit"} else (lambda v: v)
    else:
        factors = _UNIT_FACTORS.get(quantity)
        if factors is None:
            raise UnitError(f"unknown quantity {quantity!r}")
        if unit not in factors:
            raise UnitError(f"unit {unit!r} not valid for {quantity}")
        factor = factors[unit]
        convert = lambda v: v * factor  # noqa: E731

    values = [float(convert(c)) for c in candidates]
    note = None
    if len(values) > 1:
        in_range = [v for v in values if (minimum is None or v >= minimum) and (maximum is None or v <= maximum)]
        if len(in_range) != 1:
            raise UnitError(f"ambiguous number format {raw!r}")
        values = in_range
        note = f"ambiguous separator resolved by plausible range ({raw!r})"
    return ParsedQuantity(
        value=round(values[0], 3),
        unit_original=unit_text or explicit_unit or assumed_unit,
        canonical_unit=_CANONICAL[quantity],
        note=note,
    )
