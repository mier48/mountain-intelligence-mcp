from decimal import Decimal

import pytest

from mountain_intelligence.catalog.units import UnitError, parse_number, to_canonical


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("750", [Decimal(750)]),
        ("1,25", [Decimal("1.25")]),
        ("1.25", [Decimal("1.25")]),
        ("1.250,5", [Decimal("1250.5")]),
        ("1,250.5", [Decimal("1250.5")]),
        ("12.000.000", [Decimal(12000000)]),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_three_decimal_group_is_ambiguous():
    assert parse_number("1.250") == [Decimal(1250), Decimal("1.250")]


def test_ambiguity_resolved_only_by_plausible_range():
    assert to_canonical("1.250 g", "mass", minimum=300, maximum=3000).value == 1250
    with pytest.raises(UnitError, match="ambiguous"):
        to_canonical("1.250 kg", "mass", minimum=100, maximum=3000000)


@pytest.mark.parametrize(
    ("raw", "quantity", "expected"),
    [
        ("1.2 kg", "mass", 1200),
        ("26.5 oz", "mass", 751.262),
        ("2 lbs", "mass", 907.185),
        ("35L", "volume", 35),
        ("500 ml", "volume", 0.5),
        ("60 cm", "length", 600),
        ("23.6 in", "length", 599.44),
        ("20000 mm", "water_column", 20000),
        ("-4 °F", "temperature", -20),
    ],
)
def test_conversions(raw, quantity, expected):
    assert to_canonical(raw, quantity).value == pytest.approx(expected, rel=1e-4)


def test_unit_codes_and_assumed_units():
    assert to_canonical("750", "mass", explicit_unit="GRM").value == 750
    assert to_canonical(750, "mass", assumed_unit="g").value == 750


def test_missing_or_wrong_unit_is_rejected():
    with pytest.raises(UnitError, match="missing unit"):
        to_canonical("750", "mass")
    with pytest.raises(UnitError):
        to_canonical("35 l", "mass")
    with pytest.raises(UnitError):
        to_canonical(True, "mass")


def test_european_thousands_on_wide_range_is_ambiguous():
    # 20.000 mm could be 20 mm or 20000 mm within the hydrostatic-head range: never guessed.
    with pytest.raises(UnitError, match="ambiguous"):
        to_canonical("20.000 mm", "water_column", minimum=0, maximum=100000)
