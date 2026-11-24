from mountain_intelligence.catalog.normalization import normalize_specifications
from mountain_intelligence.catalog.taxonomy import CATEGORIES, get_category


def codes(result):
    return {(i.field, i.code) for i in result.issues}


def test_aliases_booleans_and_units():
    boots = get_category("mountaineering_boot")
    r = normalize_specifications(
        boots,
        {
            "weight_g": "1,6 kg",
            "weight_basis": "par",
            "rigidity": "Semi-Rigid",
            "crampon_attachment_types": "strap-on; semi-automático",
            "heel_welt": "sí",
            "toe_welt": "no",
        },
    )
    assert r.issues == []
    assert r.specifications == {
        "weight_g": 1600.0,
        "weight_basis": "per_pair",
        "rigidity": "semi_rigid",
        "crampon_attachment_types": ["strap_on", "semi_automatic"],
        "heel_welt": True,
        "toe_welt": False,
    }
    assert r.values["weight_g"].original == "1,6 kg"
    assert boots.compute_derived(r.specifications) == {"weight_per_boot_g": 800.0}


def test_null_means_unknown_not_false():
    r = normalize_specifications(get_category("mountaineering_boot"), {"insulated": None, "waterproof": ""})
    assert r.specifications == {} and r.issues == []


def test_invalid_values_are_reported_not_stored():
    r = normalize_specifications(
        get_category("crampon"),
        {
            "binding_types": ["magnetic"],
            "points": 40,
            "material": "titanium",
            "colour": "red",
            "weight_g": "900",
        },
    )
    assert r.specifications == {}
    assert codes(r) == {
        ("binding_types", "invalid_value"),
        ("points", "invalid_value"),
        ("material", "invalid_value"),
        ("colour", "unknown_field"),
        ("weight_g", "ambiguous_unit"),
    }


def test_structured_contract_units():
    r = normalize_specifications(
        get_category("backpack"), {"capacity_l": 35, "weight_g": "1150"}, units_from_field_names=True
    )
    assert r.specifications == {"capacity_l": 35.0, "weight_g": 1150.0}


def test_consistency_checks():
    r = normalize_specifications(
        get_category("mountaineering_boot"),
        {"crampon_attachment_types": ["automatic"], "toe_welt": False, "heel_welt": True},
    )
    assert ("*", "inconsistent") in codes(r)
    r = normalize_specifications(get_category("crampon"), {"boot_size_min_eu": 46, "boot_size_max_eu": 38})
    assert ("*", "inconsistent") in codes(r)


def test_derived_fields_cannot_be_ingested():
    r = normalize_specifications(get_category("mountaineering_boot"), {"weight_per_boot_g": 500})
    assert codes(r) == {("weight_per_boot_g", "unknown_field")}


def test_every_category_schema_builds():
    for definition in CATEGORIES.values():
        schema = definition.json_schema()
        assert schema["additionalProperties"] is False
        assert all(f.operators for f in definition.fields)


def test_brand_binding_system_names_are_recognised():
    crampons = get_category("crampon")
    r = normalize_specifications(crampons, {"binding_types": "New-Matic; Cramp-O-Matic; LEVERLOCK UNIVERSEL; Flexlock"})
    assert r.issues == []
    assert r.specifications["binding_types"] == ["semi_automatic", "automatic", "strap_on"]


def test_hiking_boot_schema_v2_has_rating():
    hiking = get_category("hiking_boot")
    assert hiking.schema_version == 2
    assert normalize_specifications(hiking, {"boot_rating": "b1"}).specifications == {"boot_rating": "B1"}
