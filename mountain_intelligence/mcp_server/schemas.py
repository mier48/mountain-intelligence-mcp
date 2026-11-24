"""Typed MCP tool contracts (input helpers and structured outputs).

Every tool returns the same envelope: ``status``, ``data``, ``warnings``, ``coverage``,
``pagination`` and ``meta``. Status values:

- ``ok``: complete answer.
- ``partial``: answer usable but incomplete (e.g. unknown values, unreviewed profile).
- ``not_found``: the requested product/activity/variant does not exist in the published catalog.
- ``insufficient_data``: the catalog cannot answer reliably (e.g. no candidates with known data).

Invalid arguments are reported as MCP tool errors (``isError: true``) prefixed with
``validation_error:`` so the model can correct the call. Internal errors never expose details.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mountain_intelligence.catalog.taxonomy import CATEGORIES

CategorySlug = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
Status = Literal["ok", "partial", "not_found", "insufficient_data"]


class Meta(BaseModel):
    server: str
    server_version: str
    catalog_scope: Literal["published_products", "published_products_including_synthetic_demo_data"]
    generated_at: str


class Coverage(BaseModel):
    catalog_scope: str
    matched_products: int | None = None
    data_coverage: float | None = Field(default=None, description="Share of key fields with known values (0-1)")


class Pagination(BaseModel):
    next_cursor: str | None = None
    total: int | None = None


class Envelope(BaseModel):
    status: Status
    warnings: list[str] = Field(default_factory=list)
    coverage: Coverage | None = None
    pagination: Pagination | None = None
    meta: Meta


# ---------------------------------------------------------------------------------------- inputs


class OwnedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: CategorySlug  # type: ignore[valid-type]
    product_id: str | None = Field(default=None, max_length=200, description="Catalog id if known")
    description: str | None = Field(default=None, max_length=200, description="Free description, e.g. 'old polar'")


# ---------------------------------------------------------------------------------------- outputs


class FieldInfo(BaseModel):
    name: str
    type: str
    description: str
    operators: list[str]
    unit: str | None = None
    choices: list[str] | None = None
    derived: bool | None = None
    critical: bool | None = None


class CategoryInfo(BaseModel):
    slug: str
    name_en: str
    name_es: str
    family: str
    schema_version: int
    published_products: int
    fields: list[FieldInfo]
    importance_for_activity: str | None = None


class CategoriesData(BaseModel):
    categories: list[CategoryInfo]
    activities: list[dict[str, Any]] = Field(description="Available activity profiles")


class ListCategoriesResult(Envelope):
    data: CategoriesData


class ProductSummary(BaseModel):
    product_id: str
    brand: str
    name: str
    generation: str | None = None
    category: str
    key_specs: dict[str, Any] = Field(description="Known normalized values in canonical units")
    coverage: float
    price_eur: float | None = Field(default=None, description="Lowest recent EUR offer; null if unknown")
    data_status: str
    freshness: str
    last_verified_at: str | None = None
    source_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SearchProductsResult(Envelope):
    data: list[ProductSummary]


class ProductDetailResult(Envelope):
    data: dict[str, Any] | None = Field(
        default=None, description="Full product sheet: specifications with unit/evidence, variants, offers, sources"
    )


class ComparisonRow(BaseModel):
    field: str
    unit: str | None = None
    values: dict[str, Any]
    unknown_for: list[str]
    differs: bool
    range: dict[str, Any] | None = None
    note: str | None = None


class ComparisonData(BaseModel):
    products: list[ProductSummary]
    rows: list[ComparisonRow]
    limitations: list[str]
    unknown_cells: int


class CompareProductsResult(Envelope):
    data: ComparisonData | None = None


class RecommendationData(BaseModel):
    activity: str
    category: str
    profile: dict[str, Any]
    recommended: list[dict[str, Any]]
    requires_verification: list[dict[str, Any]]
    excluded_count: int
    excluded_examples: list[dict[str, Any]]
    candidates_evaluated: int
    methodology: dict[str, Any]


class RecommendProductsResult(Envelope):
    data: RecommendationData | None = None


class CompatibilityData(BaseModel):
    relation: str
    boot: str
    crampon: str
    status: Literal["compatible", "incompatible", "conditional", "unknown"]
    reasons: list[str]
    conditions: list[str]
    contradictions: list[str]
    evidence: list[dict[str, Any]]
    rule: dict[str, Any]
    evaluated_at: str
    disclaimer: str


class CheckCompatibilityResult(Envelope):
    data: CompatibilityData | None = None


class ActivityRequirementsResult(Envelope):
    data: dict[str, Any] | None = None


class EquipmentListResult(Envelope):
    data: dict[str, Any] | None = None
