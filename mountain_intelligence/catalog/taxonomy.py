"""Declarative category taxonomy: the single source of truth for specification schemas.

Each category declares its technical fields once. From that declaration the system derives:
- the versioned Pydantic schema used on every write path (`CategoryDefinition.model`),
- the allow-listed filters and operators exposed through MCP,
- comparison rows, coverage metrics and critical-change detection.

Adding a category = adding a `CategoryDefinition` here (and bumping `schema_version` when an
existing category changes). No MCP tool needs to change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator


class FieldType(StrEnum):
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    ENUM_LIST = "enum_list"
    TEXT = "text"
    TEXT_LIST = "text_list"


OPERATORS_BY_TYPE: dict[FieldType, tuple[str, ...]] = {
    FieldType.INTEGER: ("eq", "gte", "lte", "in"),
    FieldType.NUMBER: ("eq", "gte", "lte"),
    FieldType.BOOLEAN: ("eq",),
    FieldType.ENUM: ("eq", "in"),
    FieldType.ENUM_LIST: ("contains", "contains_any", "contains_all"),
    FieldType.TEXT: ("eq",),
    FieldType.TEXT_LIST: ("contains",),
}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    description: str
    unit: str | None = None  # canonical unit of stored value
    quantity: str | None = None  # mass | length | volume | water_column | temperature
    choices: tuple[str, ...] = ()
    choice_aliases: dict[str, str] = field(default_factory=dict)  # lower-case alias -> choice
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 120
    filterable: bool = True
    critical: bool = False  # changes to published values require human review
    derived: bool = False  # computed by the system, never ingested directly
    key: bool = True  # counts towards data coverage
    lower_is_lighter: bool = False  # informational ordering for comparisons (not "better")

    @property
    def operators(self) -> tuple[str, ...]:
        return OPERATORS_BY_TYPE[self.type]

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type.value,
            "description": self.description,
            "operators": list(self.operators) if self.filterable else [],
        }
        if self.unit:
            out["unit"] = self.unit
        if self.choices:
            out["choices"] = list(self.choices)
        if self.derived:
            out["derived"] = True
        if self.critical:
            out["critical"] = True
        return out


@dataclass(frozen=True)
class CategoryDefinition:
    slug: str
    name_en: str
    name_es: str
    family: str
    schema_version: int
    fields: tuple[FieldSpec, ...]
    synonyms: tuple[str, ...] = ()
    derive: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    consistency_checks: tuple[Callable[[dict[str, Any]], str | None], ...] = ()

    @cached_property
    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    @property
    def ingestible_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if not f.derived]

    @property
    def key_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.key and not f.derived]

    @cached_property
    def model(self) -> type[BaseModel]:
        """Versioned Pydantic schema for the normalized (non-derived) specifications."""
        definitions: dict[str, Any] = {}
        validators: dict[str, Any] = {}
        for spec in self.ingestible_fields:
            definitions[spec.name] = (_python_type(spec) | None, Field(default=None, **_constraints(spec)))
            if spec.type in (FieldType.ENUM_LIST, FieldType.TEXT_LIST):
                validators[f"_dedupe_{spec.name}"] = field_validator(spec.name)(_dedupe)
        model = create_model(  # type: ignore[call-overload]
            f"{_camel(self.slug)}SpecsV{self.schema_version}",
            __config__=ConfigDict(extra="forbid", strict=False, frozen=True),
            __validators__=validators,
            **definitions,
        )
        return model

    def json_schema(self) -> dict[str, Any]:
        return self.model.model_json_schema()

    def compute_derived(self, specs: dict[str, Any]) -> dict[str, Any]:
        return self.derive(specs) if self.derive else {}

    def check_consistency(self, specs: dict[str, Any]) -> list[str]:
        return [msg for check in self.consistency_checks if (msg := check(specs))]

    def search_terms(self) -> list[str]:
        return [self.slug.replace("_", " "), self.name_en, self.name_es, *self.synonyms]


def _dedupe(value: list[Any] | None) -> list[Any] | None:
    if value is None:
        return None
    return list(dict.fromkeys(value))


def _camel(slug: str) -> str:
    return "".join(part.capitalize() for part in slug.split("_"))


def _python_type(spec: FieldSpec) -> Any:
    if spec.type is FieldType.INTEGER:
        return int
    if spec.type is FieldType.NUMBER:
        return float
    if spec.type is FieldType.BOOLEAN:
        return bool
    if spec.type is FieldType.ENUM:
        return Literal[spec.choices]  # type: ignore[valid-type]
    if spec.type is FieldType.ENUM_LIST:
        return list[Literal[spec.choices]]  # type: ignore[valid-type]
    if spec.type is FieldType.TEXT:
        return str
    return list[str]


def _constraints(spec: FieldSpec) -> dict[str, Any]:
    c: dict[str, Any] = {"description": spec.description}
    if spec.type in (FieldType.INTEGER, FieldType.NUMBER):
        if spec.minimum is not None:
            c["ge"] = spec.minimum
        if spec.maximum is not None:
            c["le"] = spec.maximum
    elif spec.type is FieldType.TEXT:
        c["max_length"] = spec.max_length
        c["min_length"] = 1
    elif spec.type in (FieldType.ENUM_LIST, FieldType.TEXT_LIST):
        c["max_length"] = 20
    return c


# --------------------------------------------------------------------------------------------
# Reusable field builders
# --------------------------------------------------------------------------------------------


def weight(minimum: float, maximum: float, description: str = "Declared weight") -> FieldSpec:
    return FieldSpec(
        "weight_g",
        FieldType.NUMBER,
        description,
        unit="g",
        quantity="mass",
        minimum=minimum,
        maximum=maximum,
        lower_is_lighter=True,
    )


def text(name: str, description: str, *, key: bool = True, critical: bool = False) -> FieldSpec:
    return FieldSpec(name, FieldType.TEXT, description, key=key, critical=critical, filterable=False)


def boolean(name: str, description: str, *, critical: bool = False, key: bool = True) -> FieldSpec:
    return FieldSpec(name, FieldType.BOOLEAN, description, critical=critical, key=key)


def enum(
    name: str,
    description: str,
    choices: tuple[str, ...],
    aliases: dict[str, str] | None = None,
    *,
    critical: bool = False,
    key: bool = True,
) -> FieldSpec:
    return FieldSpec(
        name, FieldType.ENUM, description, choices=choices, choice_aliases=aliases or {}, critical=critical, key=key
    )


def enum_list(
    name: str,
    description: str,
    choices: tuple[str, ...],
    aliases: dict[str, str] | None = None,
    *,
    critical: bool = False,
    key: bool = True,
) -> FieldSpec:
    return FieldSpec(
        name,
        FieldType.ENUM_LIST,
        description,
        choices=choices,
        choice_aliases=aliases or {},
        critical=critical,
        key=key,
    )


def standards(
    description: str = "Standards/certifications declared by the manufacturer (as declared; "
    "not independently verified)",
) -> FieldSpec:
    return FieldSpec("declared_standards", FieldType.TEXT_LIST, description, critical=True)


WEIGHT_BASIS_FOOTWEAR = enum(
    "weight_basis",
    "Whether the declared weight refers to one shoe/boot or to the pair",
    ("per_boot", "per_pair"),
    {
        "per boot": "per_boot",
        "per shoe": "per_boot",
        "single": "per_boot",
        "unit": "per_boot",
        "pair": "per_pair",
        "per pair": "per_pair",
        "par": "per_pair",
        "por par": "per_pair",
        "por unidad": "per_boot",
        "unidad": "per_boot",
    },
)
REFERENCE_SIZE_EU = FieldSpec(
    "reference_size_eu",
    FieldType.NUMBER,
    "EU size the declared weight refers to",
    minimum=30,
    maximum=52,
    filterable=False,
)
GARMENT_REFERENCE_SIZE = text("weight_reference_size", "Garment size the declared weight refers to", key=False)
GENDER_FIT = enum(
    "fit",
    "Declared fit / cut",
    ("unisex", "men", "women"),
    {
        "male": "men",
        "mens": "men",
        "men's": "men",
        "hombre": "men",
        "female": "women",
        "womens": "women",
        "women's": "women",
        "mujer": "women",
    },
    key=False,
)
CRAMPON_TYPES = ("strap_on", "semi_automatic", "automatic")
CRAMPON_TYPE_ALIASES = {
    # Brand binding systems (cross-brand equivalences documented by retailers/manufacturers):
    # Grivel New-Classic / Petzl FLEXLOCK / BD "Strap" = strap-on; Grivel New-Matic / Petzl LEVERLOCK
    # UNIVERSEL / BD "Clip" = semi-automatic; Grivel Cramp-O-Matic / Petzl LEVERLOCK FIL / BD "Pro" = automatic.
    "new-classic": "strap_on",
    "new classic": "strap_on",
    "newclassic": "strap_on",
    "flexlock": "strap_on",
    "spirlock": "strap_on",
    "new-matic": "semi_automatic",
    "new matic": "semi_automatic",
    "newmatic": "semi_automatic",
    "leverlock universel": "semi_automatic",
    "leverlock universal": "semi_automatic",
    "clip": "semi_automatic",
    "cramp-o-matic": "automatic",
    "cramp o matic": "automatic",
    "crampomatic": "automatic",
    "leverlock fil": "automatic",
    "pro": "automatic",
    "strap": "strap_on",
    "strap-on": "strap_on",
    "strap on": "strap_on",
    "universal": "strap_on",
    "basket": "strap_on",
    "classic": "strap_on",
    "correas": "strap_on",
    "de correas": "strap_on",
    "semi-automatic": "semi_automatic",
    "semiautomatic": "semi_automatic",
    "semi automatic": "semi_automatic",
    "hybrid": "semi_automatic",
    "semi-auto": "semi_automatic",
    "semiautomático": "semi_automatic",
    "semiautomatico": "semi_automatic",
    "semi-automático": "semi_automatic",
    "automatic": "automatic",
    "step-in": "automatic",
    "step in": "automatic",
    "clip-on": "automatic",
    "automático": "automatic",
    "automatico": "automatic",
}
RIGIDITY = enum(
    "rigidity",
    "Sole rigidity as declared by the manufacturer",
    ("flexible", "semi_rigid", "rigid"),
    {
        "semi-rigid": "semi_rigid",
        "semirigid": "semi_rigid",
        "semirrígida": "semi_rigid",
        "semirrigida": "semi_rigid",
        "rígida": "rigid",
        "rigida": "rigid",
        "flexible": "flexible",
    },
)


def _weight_per_unit(per_unit_key: str, unit_basis: str, pair_basis: str) -> Callable[[dict], dict]:
    def derive(specs: dict[str, Any]) -> dict[str, Any]:
        w, basis = specs.get("weight_g"), specs.get("weight_basis")
        if w is None or basis is None:
            return {}
        if basis == unit_basis:
            return {per_unit_key: w}
        if basis == pair_basis:
            return {per_unit_key: round(w / 2, 1)}
        return {}

    return derive


def _range_check(lo: str, hi: str) -> Callable[[dict[str, Any]], str | None]:
    def check(specs: dict[str, Any]) -> str | None:
        a, b = specs.get(lo), specs.get(hi)
        if a is not None and b is not None and a > b:
            return f"{lo} ({a}) is greater than {hi} ({b})"
        return None

    return check


def _welt_requirements(specs: dict[str, Any]) -> str | None:
    """A boot declaring automatic/semi-automatic compatibility while declaring no welt is contradictory."""
    types = specs.get("crampon_attachment_types") or []
    if "automatic" in types and (specs.get("toe_welt") is False or specs.get("heel_welt") is False):
        return "declares automatic crampon compatibility but also declares a missing toe/heel welt"
    if "semi_automatic" in types and specs.get("heel_welt") is False:
        return "declares semi-automatic crampon compatibility but also declares no heel welt"
    return None


def _fill_power_only_down(specs: dict[str, Any]) -> str | None:
    if specs.get("fill_power") is not None and specs.get("insulation_type") == "synthetic":
        return "fill_power is only meaningful for down insulation"
    return None


# --------------------------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------------------------

MOUNTAINEERING_BOOT = CategoryDefinition(
    slug="mountaineering_boot",
    name_en="Mountaineering boot",
    name_es="Bota de alpinismo",
    family="footwear",
    schema_version=1,
    synonyms=(
        "botas de alpinismo",
        "bota alpinismo",
        "botas de montaña invernal",
        "mountaineering boots",
        "alpine boots",
        "botas alpinismo",
        "bota de montaña",
        "boots",
    ),
    fields=(
        weight(300, 3000, "Declared weight (see weight_basis and reference_size_eu)"),
        WEIGHT_BASIS_FOOTWEAR,
        REFERENCE_SIZE_EU,
        FieldSpec(
            "weight_per_boot_g",
            FieldType.NUMBER,
            "Weight of one boot derived from the declared weight and its basis",
            unit="g",
            derived=True,
            lower_is_lighter=True,
        ),
        RIGIDITY,
        enum(
            "boot_rating",
            "B-rating declared by the manufacturer (B0-B3 scheme)",
            ("B0", "B1", "B2", "B3"),
            {"b0": "B0", "b1": "B1", "b2": "B2", "b3": "B3"},
            critical=True,
        ),
        enum_list(
            "crampon_attachment_types",
            "Crampon binding types the boot manufacturer declares compatible",
            CRAMPON_TYPES,
            CRAMPON_TYPE_ALIASES,
            critical=True,
        ),
        boolean("toe_welt", "Declared toe welt suitable for crampon toe bails", critical=True),
        boolean("heel_welt", "Declared heel welt suitable for crampon heel levers", critical=True),
        boolean("waterproof", "Declared waterproof (does not imply any specific test value)"),
        text("membrane", "Declared waterproof membrane"),
        boolean("insulated", "Declared thermal insulation"),
        text("insulation_description", "Declared insulation material/description", key=False),
        boolean("integrated_gaiter", "Integrated gaiter/sock cuff", key=False),
        text("upper_material", "Declared upper material", key=False),
        text("outsole", "Declared outsole", key=False),
    ),
    derive=_weight_per_unit("weight_per_boot_g", "per_boot", "per_pair"),
    consistency_checks=(_welt_requirements,),
)

HIKING_BOOT = CategoryDefinition(
    slug="hiking_boot",
    name_en="Hiking boot",
    name_es="Bota de trekking",
    family="footwear",
    schema_version=2,  # v2: adds boot_rating
    synonyms=("botas de trekking", "bota trekking", "botas de senderismo", "hiking boots", "trekking boots"),
    fields=(
        weight(200, 2500),
        WEIGHT_BASIS_FOOTWEAR,
        REFERENCE_SIZE_EU,
        FieldSpec(
            "weight_per_boot_g",
            FieldType.NUMBER,
            "Derived weight of one boot",
            unit="g",
            derived=True,
            lower_is_lighter=True,
        ),
        RIGIDITY,
        enum(
            "boot_rating",
            "B-rating declared by the manufacturer (B0 = not for crampons)",
            ("B0", "B1", "B2", "B3"),
            {"b0": "B0", "b1": "B1", "b2": "B2", "b3": "B3"},
            critical=True,
        ),
        enum("cuff_height", "Cuff height", ("low", "mid", "high"), {"baja": "low", "media": "mid", "alta": "high"}),
        boolean("waterproof", "Declared waterproof (does not imply any specific test value)"),
        text("membrane", "Declared waterproof membrane"),
        enum_list(
            "crampon_attachment_types",
            "Crampon binding types declared compatible",
            CRAMPON_TYPES,
            CRAMPON_TYPE_ALIASES,
            critical=True,
        ),
        text("outsole", "Declared outsole", key=False),
    ),
    derive=_weight_per_unit("weight_per_boot_g", "per_boot", "per_pair"),
)

TRAIL_RUNNING_SHOE = CategoryDefinition(
    slug="trail_running_shoe",
    name_en="Trail running shoe",
    name_es="Zapatilla de trail running",
    family="footwear",
    schema_version=1,
    synonyms=("zapatillas de trail", "zapatilla trail", "trail shoes", "trail running shoes", "zapatillas de montaña"),
    fields=(
        weight(100, 800),
        WEIGHT_BASIS_FOOTWEAR,
        REFERENCE_SIZE_EU,
        FieldSpec(
            "weight_per_boot_g",
            FieldType.NUMBER,
            "Derived weight of one shoe",
            unit="g",
            derived=True,
            lower_is_lighter=True,
        ),
        FieldSpec("drop_mm", FieldType.NUMBER, "Heel-to-toe drop", unit="mm", quantity="length", minimum=0, maximum=20),
        FieldSpec(
            "lug_depth_mm", FieldType.NUMBER, "Outsole lug depth", unit="mm", quantity="length", minimum=0, maximum=12
        ),
        enum_list(
            "terrain",
            "Terrain declared by the manufacturer",
            ("road", "trail", "technical", "mud", "snow"),
            {
                "técnico": "technical",
                "tecnico": "technical",
                "barro": "mud",
                "nieve": "snow",
                "asfalto": "road",
                "sendero": "trail",
            },
        ),
        boolean("waterproof", "Declared waterproof"),
        text("membrane", "Declared membrane", key=False),
    ),
    derive=_weight_per_unit("weight_per_boot_g", "per_boot", "per_pair"),
)

HARDSHELL_JACKET = CategoryDefinition(
    slug="hardshell_jacket",
    name_en="Waterproof hardshell jacket",
    name_es="Chaqueta impermeable",
    family="apparel",
    schema_version=1,
    synonyms=(
        "chaqueta impermeable",
        "chaquetas impermeables",
        "hardshell",
        "shell",
        "rain jacket",
        "waterproof jacket",
        "chubasquero",
        "cortavientos impermeable",
        "gore-tex",
    ),
    fields=(
        weight(80, 1500, "Declared weight (see weight_reference_size)"),
        GARMENT_REFERENCE_SIZE,
        text("membrane", "Declared waterproof-breathable membrane"),
        enum(
            "construction",
            "Laminate construction",
            ("2_layer", "2_5_layer", "3_layer"),
            {
                "2l": "2_layer",
                "2 layer": "2_layer",
                "2-layer": "2_layer",
                "2.5l": "2_5_layer",
                "2.5 layer": "2_5_layer",
                "2.5-layer": "2_5_layer",
                "3l": "3_layer",
                "3 layer": "3_layer",
                "3-layer": "3_layer",
                "3 capas": "3_layer",
                "2 capas": "2_layer",
                "2,5 capas": "2_5_layer",
            },
        ),
        FieldSpec(
            "waterproof_rating_mm",
            FieldType.NUMBER,
            "Declared hydrostatic head (mm water column)",
            unit="mm",
            quantity="water_column",
            minimum=0,
            maximum=100000,
        ),
        text("waterproof_test_method", "Test method for waterproof_rating_mm (e.g. ISO 811)", key=False),
        FieldSpec(
            "breathability_ret",
            FieldType.NUMBER,
            "Declared RET (m²·Pa/W). Lower = less resistance. Not comparable with MVTR",
            unit="m2pa_w",
            minimum=0,
            maximum=100,
        ),
        FieldSpec(
            "breathability_mvtr",
            FieldType.NUMBER,
            "Declared MVTR (g/m²/24h). Protocol-dependent; not comparable with RET",
            unit="g_m2_24h",
            minimum=0,
            maximum=100000,
        ),
        text(
            "breathability_test_method",
            "Declared breathability test protocol (e.g. ISO 11092, JIS L 1099 B1)",
            key=False,
        ),
        enum_list(
            "ventilation",
            "Declared ventilation features",
            ("pit_zips", "mesh_pockets", "two_way_front_zip"),
            {
                "pit zips": "pit_zips",
                "pit-zips": "pit_zips",
                "underarm zips": "pit_zips",
                "ventilación axilar": "pit_zips",
                "cremalleras de ventilación": "pit_zips",
            },
        ),
        enum(
            "hood",
            "Hood type",
            ("none", "fixed", "helmet_compatible", "stowable"),
            {
                "helmet compatible": "helmet_compatible",
                "compatible con casco": "helmet_compatible",
                "sin capucha": "none",
                "fija": "fixed",
            },
        ),
        text("face_fabric", "Declared face fabric", key=False),
        GENDER_FIT,
    ),
)

MIDLAYER = CategoryDefinition(
    slug="midlayer",
    name_en="Midlayer",
    name_es="Capa intermedia",
    family="apparel",
    schema_version=1,
    synonyms=("forro polar", "polar", "fleece", "midlayer", "capa intermedia", "segunda capa"),
    fields=(
        weight(50, 1200),
        GARMENT_REFERENCE_SIZE,
        enum(
            "fabric_type",
            "Fabric type",
            ("fleece", "grid_fleece", "active_insulation", "softshell", "wool"),
            {"polar": "fleece", "grid": "grid_fleece", "lana": "wool", "merino": "wool"},
        ),
        text("material", "Declared material"),
        FieldSpec("fabric_weight_gsm", FieldType.NUMBER, "Fabric weight (g/m²)", unit="g_m2", minimum=20, maximum=600),
        boolean("hood", "Has hood"),
        enum("zip", "Front zip", ("full", "half", "quarter", "none"), {"completa": "full", "media": "half"}),
        GENDER_FIT,
    ),
)

INSULATED_JACKET = CategoryDefinition(
    slug="insulated_jacket",
    name_en="Insulated jacket",
    name_es="Chaqueta aislante",
    family="apparel",
    schema_version=1,
    synonyms=(
        "chaqueta de plumas",
        "plumas",
        "plumífero",
        "plumifero",
        "down jacket",
        "insulated jacket",
        "chaqueta sintética",
        "puffer",
    ),
    fields=(
        weight(80, 2000),
        GARMENT_REFERENCE_SIZE,
        enum(
            "insulation_type",
            "Insulation type",
            ("down", "synthetic", "hybrid"),
            {
                "plumón": "down",
                "pluma": "down",
                "plumas": "down",
                "sintético": "synthetic",
                "sintetico": "synthetic",
                "híbrido": "hybrid",
            },
        ),
        FieldSpec(
            "fill_weight_g",
            FieldType.NUMBER,
            "Declared insulation fill weight",
            unit="g",
            quantity="mass",
            minimum=10,
            maximum=1000,
        ),
        FieldSpec(
            "fill_power",
            FieldType.INTEGER,
            "Declared fill power (down only; standard-dependent)",
            minimum=300,
            maximum=1000,
        ),
        text("fill_power_method", "Fill power test standard (e.g. IDFB, US/EU cuin)", key=False),
        boolean("hood", "Has hood"),
        text("construction", "Declared baffle/quilt construction", key=False),
        GENDER_FIT,
    ),
    consistency_checks=(_fill_power_only_down,),
)

BACKPACK = CategoryDefinition(
    slug="backpack",
    name_en="Backpack",
    name_es="Mochila",
    family="packs",
    schema_version=1,
    synonyms=("mochila", "mochilas", "backpack", "pack", "rucksack", "mochila de alpinismo", "mochila de montaña"),
    fields=(
        FieldSpec(
            "capacity_l", FieldType.NUMBER, "Declared capacity", unit="l", quantity="volume", minimum=1, maximum=150
        ),
        weight(50, 5000, "Declared weight (see weight_reference_size)"),
        text("weight_reference_size", "Back size the declared weight refers to", key=False),
        FieldSpec("back_sizes", FieldType.TEXT_LIST, "Declared back length sizes", key=False),
        FieldSpec(
            "max_load_g",
            FieldType.NUMBER,
            "Recommended maximum load declared by the manufacturer",
            unit="g",
            quantity="mass",
            minimum=1000,
            maximum=60000,
        ),
        FieldSpec(
            "ice_axe_attachments", FieldType.INTEGER, "Number of declared ice-axe attachments", minimum=0, maximum=4
        ),
        boolean("rope_attachment", "Declared rope attachment"),
        enum_list(
            "ski_carry",
            "Declared ski carry systems",
            ("a_frame", "diagonal"),
            {"a-frame": "a_frame", "a frame": "a_frame", "diagonal": "diagonal"},
        ),
        enum_list(
            "access",
            "Main compartment access",
            ("top", "front_panel", "side", "back_panel", "bottom"),
            {
                "superior": "top",
                "frontal": "front_panel",
                "lateral": "side",
                "trasera": "back_panel",
                "inferior": "bottom",
                "panel": "front_panel",
            },
        ),
        enum(
            "hip_belt",
            "Hip belt type",
            ("none", "webbing", "padded", "removable_padded"),
            {"removable": "removable_padded", "acolchado": "padded", "cinta": "webbing"},
        ),
        enum(
            "back_system",
            "Declared back system",
            ("frameless", "framesheet", "internal_frame", "suspended_mesh"),
            {"mesh": "suspended_mesh", "trampoline": "suspended_mesh", "frame sheet": "framesheet"},
        ),
        boolean("hydration_compatible", "Hydration reservoir compatible", key=False),
        boolean("rain_cover_included", "Rain cover included", key=False),
        GENDER_FIT,
    ),
)

CRAMPON = CategoryDefinition(
    slug="crampon",
    name_en="Crampon",
    name_es="Crampón",
    family="technical_hardware",
    schema_version=1,
    synonyms=("crampones", "crampon", "crampons", "crampón"),
    fields=(
        enum_list(
            "binding_types",
            "Binding configurations declared by the crampon manufacturer",
            CRAMPON_TYPES,
            CRAMPON_TYPE_ALIASES,
            critical=True,
        ),
        enum(
            "crampon_rating",
            "C-rating declared by the manufacturer (C1-C3 scheme)",
            ("C1", "C2", "C3"),
            {"c1": "C1", "c2": "C2", "c3": "C3"},
            critical=True,
        ),
        FieldSpec("points", FieldType.INTEGER, "Number of points", minimum=4, maximum=14),
        enum(
            "front_points",
            "Front point configuration",
            ("horizontal_dual", "vertical_dual", "mono", "convertible"),
            {"dual": "horizontal_dual", "monopunta": "mono", "mono-point": "mono", "monopoint": "mono"},
        ),
        enum(
            "material",
            "Main material",
            ("steel", "stainless_steel", "aluminium"),
            {
                "acero": "steel",
                "acero inoxidable": "stainless_steel",
                "stainless": "stainless_steel",
                "aluminum": "aluminium",
                "aluminio": "aluminium",
            },
        ),
        weight(150, 1500, "Declared weight (see weight_basis)"),
        enum(
            "weight_basis",
            "Whether weight refers to the pair or one crampon",
            ("per_pair", "per_crampon"),
            {
                "pair": "per_pair",
                "par": "per_pair",
                "per pair": "per_pair",
                "single": "per_crampon",
                "unidad": "per_crampon",
            },
        ),
        FieldSpec(
            "weight_per_pair_g",
            FieldType.NUMBER,
            "Derived weight of the pair",
            unit="g",
            derived=True,
            lower_is_lighter=True,
        ),
        FieldSpec(
            "boot_size_min_eu",
            FieldType.NUMBER,
            "Smallest declared compatible boot size (EU)",
            minimum=30,
            maximum=52,
            critical=True,
        ),
        FieldSpec(
            "boot_size_max_eu",
            FieldType.NUMBER,
            "Largest declared compatible boot size (EU)",
            minimum=30,
            maximum=52,
            critical=True,
        ),
        boolean("anti_balling_plates", "Anti-balling plates included"),
        standards(),
    ),
    derive=lambda s: (
        {"weight_per_pair_g": s["weight_g"] if s.get("weight_basis") == "per_pair" else s["weight_g"] * 2}
        if s.get("weight_g") is not None and s.get("weight_basis")
        else {}
    ),
    consistency_checks=(_range_check("boot_size_min_eu", "boot_size_max_eu"),),
)

ICE_AXE = CategoryDefinition(
    slug="ice_axe",
    name_en="Ice axe",
    name_es="Piolet",
    family="technical_hardware",
    schema_version=1,
    synonyms=("piolet", "piolets", "ice axe", "ice axes"),
    fields=(
        enum(
            "intended_use",
            "Declared intended use",
            ("walking_mountaineering", "technical_mountaineering", "ice_climbing", "ski_touring"),
            {
                "alpinismo clásico": "walking_mountaineering",
                "classic mountaineering": "walking_mountaineering",
                "alpinismo técnico": "technical_mountaineering",
                "escalada en hielo": "ice_climbing",
                "esquí de montaña": "ski_touring",
            },
        ),
        FieldSpec("length_mm", FieldType.NUMBER, "Length", unit="mm", quantity="length", minimum=300, maximum=1000),
        weight(150, 1500),
        enum(
            "shaft", "Shaft shape", ("straight", "curved", "reverse_curved"), {"recto": "straight", "curvo": "curved"}
        ),
        enum(
            "en13089_shaft_type",
            "EN 13089 shaft type declared (B=basic, T=technical)",
            ("B", "T"),
            {"1": "B", "2": "T", "type 1": "B", "type 2": "T"},
            critical=True,
        ),
        enum(
            "en13089_head_type",
            "EN 13089 head type declared (B=basic, T=technical)",
            ("B", "T"),
            {"1": "B", "2": "T", "type 1": "B", "type 2": "T"},
            critical=True,
        ),
        standards(),
    ),
)

CLIMBING_HELMET = CategoryDefinition(
    slug="climbing_helmet",
    name_en="Climbing helmet",
    name_es="Casco de escalada",
    family="protection",
    schema_version=1,
    synonyms=("casco", "casco de escalada", "casco de montaña", "climbing helmet", "helmet"),
    fields=(
        weight(100, 800),
        FieldSpec(
            "head_circumference_min_mm",
            FieldType.NUMBER,
            "Smallest declared head circumference",
            unit="mm",
            quantity="length",
            minimum=400,
            maximum=700,
        ),
        FieldSpec(
            "head_circumference_max_mm",
            FieldType.NUMBER,
            "Largest declared head circumference",
            unit="mm",
            quantity="length",
            minimum=400,
            maximum=700,
        ),
        enum(
            "construction",
            "Shell construction",
            ("hardshell", "in_mold", "hybrid"),
            {"in-mold": "in_mold", "abs": "hardshell", "hard shell": "hardshell", "híbrido": "hybrid"},
        ),
        boolean("ventilated", "Declared ventilation openings", key=False),
        standards(),
    ),
    consistency_checks=(_range_check("head_circumference_min_mm", "head_circumference_max_mm"),),
)

CATEGORIES: dict[str, CategoryDefinition] = {
    c.slug: c
    for c in (
        MOUNTAINEERING_BOOT,
        HIKING_BOOT,
        TRAIL_RUNNING_SHOE,
        HARDSHELL_JACKET,
        MIDLAYER,
        INSULATED_JACKET,
        BACKPACK,
        CRAMPON,
        ICE_AXE,
        CLIMBING_HELMET,
    )
}


def get_category(slug: str) -> CategoryDefinition:
    try:
        return CATEGORIES[slug]
    except KeyError as exc:
        raise KeyError(f"unknown category {slug!r}") from exc
