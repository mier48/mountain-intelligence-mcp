"""MCP server definition: the nine read-only tools.

Tools are synchronous functions; the SDK runs them in worker threads, where Django's ORM is
safe to use (no event loop in that thread). Connections are recycled around each call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Count, Q
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mountain_intelligence import __version__
from mountain_intelligence.catalog import queries
from mountain_intelligence.catalog.models import Category, DataStatus, Product
from mountain_intelligence.catalog.queries import NotFoundError, QueryValidationError, SpecFilter
from mountain_intelligence.catalog.taxonomy import CATEGORIES
from mountain_intelligence.recommendations import compatibility, engine, equipment
from mountain_intelligence.recommendations.profiles import Conditions

from .schemas import (
    ActivityRequirementsResult,
    BrandInfo,
    CategoriesData,
    CategoryInfo,
    CategorySlug,
    CheckCompatibilityResult,
    CompareProductsResult,
    Coverage,
    EquipmentListResult,
    FieldInfo,
    ListBrandsResult,
    ListCategoriesResult,
    Meta,
    OwnedItem,
    Pagination,
    ProductDetailResult,
    RecommendProductsResult,
    SearchProductsResult,
    TechnicalCategorySlug,
)

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Mountain Intelligence: read-only catalog of mountain/outdoor equipment. Technical categories (boots,
crampons, packs, shells...) carry validated specifications; manufacturers' full catalogs also include
`uncategorized` products (catalog data only: manufacturer category, type, tags, variants, SKU/GTIN,
prices, availability, images, original declared attributes). Use the tools instead of guessing.

Workflow: list_categories (fields, units, operators) / list_brands -> search_products / get_product ->
compare_products, recommend_products, check_compatibility, get_activity_requirements,
build_equipment_list. Answers are structured and traceable (sources, dates, evidence status).

Rules for the assistant: values are normalized to canonical units (g, mm, l, EUR). null means
UNKNOWN (never false/zero). Respect `warnings`: unreviewed activity profiles, synthetic demo data,
stale data or pending reviews must be mentioned to the user. `source_attributes` are the manufacturer's
own unvalidated statements. Uncategorized products are never recommended, compared or checked for
compatibility. Compatibility 'conditional' is NOT a confirmation. Nothing here assesses whether a route
or activity is safe. Queries may be in Spanish or English; product names keep their original language.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

ActivityKey = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]{2,79}$",
        description="Activity profile key, e.g. winter_mountaineering (see list_categories)",
    ),
]
ProductId = Annotated[str, Field(min_length=3, max_length=200, description="Stable product_id from search results")]
Experience = Literal["beginner", "intermediate", "advanced"]


def _meta() -> Meta:
    return Meta(
        server=settings.MCP_SERVER_NAME,
        server_version=__version__,
        catalog_scope=(
            "published_products_including_synthetic_demo_data"
            if settings.MI_INCLUDE_SYNTHETIC_DATA
            else "published_products"
        ),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _scope() -> str:
    return _meta().catalog_scope


def _run[R](fn: Callable[[], R], tool: str) -> R:
    """Run a tool body with DB connection hygiene and uniform error mapping."""
    close_old_connections()
    try:
        return fn()
    except QueryValidationError as exc:
        raise ToolError(f"validation_error: {exc}") from exc
    finally:
        close_old_connections()


def _status(warnings: list[str], *, partial: bool = False) -> Literal["ok", "partial"]:
    return "partial" if partial or warnings else "ok"


def build_server() -> MCPServer:
    mcp = MCPServer(
        settings.MCP_SERVER_NAME,
        title="Mountain Intelligence",
        description="Traceable technical catalog and rule-based recommendations for mountain equipment.",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )

    @mcp.tool(annotations=READ_ONLY, title="List categories and filterable fields")
    def list_categories(
        activity: Annotated[
            ActivityKey | None, Field(description="Optional: only categories used by this activity")
        ] = None,
    ) -> ListCategoriesResult:
        """List equipment categories with their filterable fields (type, canonical unit, operators, allowed
        values) and the available activity profiles. Call this before using `filters` in search_products or
        `required_features` in recommend_products."""

        def body() -> ListCategoriesResult:
            warnings: list[str] = []
            importance: dict[str, str] = {}
            if activity:
                try:
                    _, pdef = engine.resolve_profile(activity)
                except NotFoundError as exc:
                    return ListCategoriesResult(
                        status="not_found",
                        warnings=[str(exc)],
                        meta=_meta(),
                        data=CategoriesData(categories=[], activities=equipment.list_activities()),
                    )
                importance = {n.category: n.importance for n in pdef.needs}
            visible = Q(products__publication_status=Product.PublicationStatus.PUBLISHED)
            if not settings.MI_INCLUDE_SYNTHETIC_DATA:
                visible &= ~Q(products__data_status=DataStatus.SYNTHETIC_FIXTURE)
            counts = dict(Category.objects.annotate(n=Count("products", filter=visible)).values_list("slug", "n"))
            categories = []
            for slug, definition in CATEGORIES.items():
                if activity and slug not in importance:
                    continue
                categories.append(
                    CategoryInfo(
                        slug=slug,
                        name_en=definition.name_en,
                        name_es=definition.name_es,
                        family=definition.family,
                        schema_version=definition.schema_version,
                        technical=definition.technical,
                        published_products=counts.get(slug, 0),
                        fields=[FieldInfo(**f.describe()) for f in definition.fields],
                        importance_for_activity=importance.get(slug),
                    )
                )
            if sum(c.published_products for c in categories) == 0:
                warnings.append("the published catalog is empty for this scope")
            return ListCategoriesResult(
                status=_status(warnings),
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(
                    catalog_scope=_scope(), matched_products=sum(c.published_products for c in categories)
                ),
                data=CategoriesData(categories=categories, activities=equipment.list_activities()),
            )

        return _run(body, "list_categories")

    @mcp.tool(annotations=READ_ONLY, title="List brands")
    def list_brands(
        category: Annotated[
            CategorySlug | None,  # type: ignore[valid-type]
            Field(description="Optional: only brands with published products in this category"),
        ] = None,
    ) -> ListBrandsResult:
        """List the brands in the catalog with their published product count and the categories they cover.
        Without `category` it also lists registered brands that have no published products yet. Use the
        returned `name` in the `brands` argument of search_products."""

        def body() -> ListBrandsResult:
            items = queries.list_brands(category=category)
            warnings: list[str] = []
            total = sum(i["published_products"] for i in items)
            if any(i["synthetic"] for i in items):
                warnings.append("includes SYNTHETIC demo brands, not real ones")
            if total == 0:
                warnings.append(
                    f"no published products in {category}"
                    if category
                    else "the published catalog is empty for this scope"
                )
            return ListBrandsResult(
                status="insufficient_data" if total == 0 else _status(warnings),
                data=[BrandInfo(**i) for i in items],
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=total),
                pagination=Pagination(total=len(items)),
            )

        return _run(body, "list_brands")

    @mcp.tool(annotations=READ_ONLY, title="Search products")
    def search_products(
        query: Annotated[
            str,
            Field(max_length=200, description="Free text (es/en): model, brand, SKU/GTIN, category words. Optional."),
        ] = "",
        category: Annotated[CategorySlug | None, Field(description="Category slug; required when using filters")] =  # type: ignore[valid-type]
        None,
        brands: Annotated[list[str] | None, Field(max_length=20, description="Brand names to include")] = None,
        filters: Annotated[
            list[SpecFilter] | None,
            Field(
                max_length=12,
                description="Typed filters, e.g. {field:'weight_per_boot_g', op:'lte', value:800}. "
                "Unknown values never match a filter.",
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        cursor: Annotated[str | None, Field(max_length=200, description="next_cursor from a previous page")] = None,
        classification: Annotated[
            Literal["any", "technical", "uncategorized"],
            Field(description="Restrict to technically classified products or to `uncategorized` ones"),
        ] = "any",
    ) -> SearchProductsResult:
        """Search the published catalog combining text (Spanish/English synonyms, SKU/MPN/GTIN exact match,
        manufacturer category/type/tags) with typed specification filters and stable pagination. Returns
        compact summaries with known key specifications, technical classification, data coverage, lowest
        recent EUR price (if any), freshness and source URL."""

        def body() -> SearchProductsResult:
            result = queries.search_products(
                query=query,
                category=category,
                brands=brands,
                filters=filters,
                limit=limit,
                cursor=cursor,
                classification=classification,
            )
            warnings = result["warnings"]
            status = "ok" if result["items"] else "insufficient_data"
            if not result["items"]:
                warnings.append("no published product matches; relax filters or check list_categories")
            elif warnings:
                status = "partial"
            avg = (
                round(sum(i["coverage"] for i in result["items"]) / len(result["items"]), 2)
                if result["items"]
                else None
            )
            return SearchProductsResult(
                status=status,
                data=result["items"],
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=result["total"], data_coverage=avg),
                pagination=Pagination(next_cursor=result["next_cursor"], total=result["total"]),
            )

        return _run(body, "search_products")

    @mcp.tool(annotations=READ_ONLY, title="Get product sheet")
    def get_product(
        product_id: ProductId,
        variant: Annotated[str | None, Field(max_length=80, description="Optional SKU/GTIN/MPN of a variant")] = None,
        include_evidence: bool = True,
    ) -> ProductDetailResult:
        """Full sheet of one product: every schema field with value or null (unknown), canonical unit,
        per-field evidence (source URL, observation date, verification status), manufacturer metadata
        (original category, type, tags, collections, identifiers, images, declared attributes), variants with
        SKU/GTIN/options, recent offers and availability, sources, freshness and review warnings. Works for
        `uncategorized` products too (no specifications, catalog data only)."""

        def body() -> ProductDetailResult:
            try:
                detail = queries.product_detail(product_id, variant=variant, include_evidence=include_evidence)
            except NotFoundError as exc:
                return ProductDetailResult(status="not_found", warnings=[str(exc)], meta=_meta())
            warnings = detail.pop("warnings")
            return ProductDetailResult(
                status=_status(warnings, partial=detail["coverage"] < 1),
                data=detail,
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=1, data_coverage=detail["coverage"]),
            )

        return _run(body, "get_product")

    @mcp.tool(annotations=READ_ONLY, title="Compare products")
    def compare_products(
        product_ids: Annotated[list[str], Field(min_length=2, max_length=5, description="2-5 product ids")],
    ) -> CompareProductsResult:
        """Side-by-side matrix of homologous fields for 2-5 technically classified products of the same
        category/family (uncategorized products cannot be compared). Unknown
        values are explicit; limitations flag non-comparable data (weight per unit vs per pair, different
        reference sizes, RET vs MVTR, different test protocols)."""

        def body() -> CompareProductsResult:
            try:
                data = queries.compare_products(product_ids)
            except NotFoundError as exc:
                return CompareProductsResult(status="not_found", warnings=[str(exc)], meta=_meta())
            warnings = list(data["limitations"])
            for p in data["products"]:
                warnings.extend(f"{p['product_id']}: {w}" for w in p["warnings"])
            return CompareProductsResult(
                status=_status(warnings, partial=data["unknown_cells"] > 0),
                data=data,
                warnings=list(dict.fromkeys(warnings)),
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=len(data["products"])),
            )

        return _run(body, "compare_products")

    @mcp.tool(annotations=READ_ONLY, title="Recommend products for an activity")
    def recommend_products(
        activity: ActivityKey,
        category: TechnicalCategorySlug,  # type: ignore[valid-type]
        conditions: Annotated[Conditions | None, Field(description="Known conditions; omit unknown values")] = None,
        experience: Experience | None = None,
        budget_eur: Annotated[float | None, Field(gt=0, le=20000)] = None,
        required_features: Annotated[
            list[SpecFilter] | None,
            Field(max_length=12, description="Mandatory user requirements (same syntax as search filters)"),
        ] = None,
        preferred_brands: Annotated[list[str] | None, Field(max_length=10)] = None,
        owned_product_ids: Annotated[list[str] | None, Field(max_length=30)] = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> RecommendProductsResult:
        """Deterministic, explained recommendation. Mandatory profile requirements, required_features and
        budget EXCLUDE candidates; candidates with unknown mandatory data or unknown price (when a budget is
        given) are returned separately in `requires_verification`, never as recommended; so are candidates
        without any known key specification. Uncategorized products are never candidates. Ranking uses
        versioned weights (technical fit, weight, price, preferences) with a per-candidate breakdown.
        The assistant writes the final advice; this tool does not assess route safety."""

        def body() -> RecommendProductsResult:
            try:
                data = engine.recommend(
                    activity=activity,
                    category=category,
                    conditions=conditions,
                    experience=experience,
                    budget_eur=budget_eur,
                    required_features=required_features,
                    preferred_brands=preferred_brands,
                    owned_product_ids=owned_product_ids,
                    limit=limit,
                )
            except NotFoundError as exc:
                return RecommendProductsResult(status="not_found", warnings=[str(exc)], meta=_meta())
            warnings = data.pop("warnings")
            if not data["recommended"]:
                status: Any = "insufficient_data"
                warnings.append("no candidate satisfies every mandatory requirement with known data")
            else:
                status = _status(warnings)
            return RecommendProductsResult(
                status=status,
                data=data,
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=data["candidates_evaluated"]),
            )

        return _run(body, "recommend_products")

    @mcp.tool(annotations=READ_ONLY, title="Check boot/crampon compatibility")
    def check_compatibility(
        product_ids: Annotated[list[str], Field(min_length=2, max_length=2, description="One boot and one crampon")],
        relation: Literal["boot_crampon"] = "boot_crampon",
        boot_size_eu: Annotated[float | None, Field(ge=30, le=52, description="User's boot size, if known")] = None,
    ) -> CheckCompatibilityResult:
        """Evidence-based compatibility with four states: compatible (documented pair statement),
        incompatible (documented exclusion), conditional (binding types match but the concrete pair is not
        confirmed: fit/size checks required) and unknown (missing or contradictory evidence). Always relay the
        conditions and the physical-check disclaimer."""

        def body() -> CheckCompatibilityResult:
            try:
                data = compatibility.check_compatibility(
                    product_ids,
                    relation=relation,
                    boot_size_eu=boot_size_eu,
                    include_synthetic=settings.MI_INCLUDE_SYNTHETIC_DATA,
                )
            except NotFoundError as exc:
                return CheckCompatibilityResult(status="not_found", warnings=[str(exc)], meta=_meta())
            warnings = []
            if data["status"] in ("unknown",):
                warnings.append("compatibility could not be established from the available evidence")
            if data["status"] == "conditional":
                warnings.append("conditional: binding types match, the specific pair is NOT confirmed")
            status = "ok" if data["status"] in ("compatible", "incompatible") else "partial"
            return CheckCompatibilityResult(
                status=status,
                data=data,
                warnings=warnings,
                meta=_meta(),
                coverage=Coverage(catalog_scope=_scope(), matched_products=2),
            )

        return _run(body, "check_compatibility")

    @mcp.tool(annotations=READ_ONLY, title="Activity equipment requirements")
    def get_activity_requirements(
        activity: ActivityKey,
        conditions: Conditions | None = None,
        experience: Experience | None = None,
    ) -> ActivityRequirementsResult:
        """Orientative equipment requirements for an activity profile and context: needed categories with
        importance, mandatory requirements, preferences, rationale, missing context and the profile's review
        status. It is not a safety assessment of any route."""

        def body() -> ActivityRequirementsResult:
            try:
                data = equipment.activity_requirements(activity=activity, conditions=conditions, experience=experience)
            except NotFoundError as exc:
                return ActivityRequirementsResult(
                    status="not_found",
                    warnings=[str(exc)],
                    meta=_meta(),
                    data={"available_activities": equipment.list_activities()},
                )
            warnings = data.pop("warnings")
            return ActivityRequirementsResult(
                status=_status(warnings, partial=bool(data["missing_context"])),
                data=data,
                warnings=warnings,
                meta=_meta(),
            )

        return _run(body, "get_activity_requirements")

    @mcp.tool(annotations=READ_ONLY, title="Build an equipment checklist")
    def build_equipment_list(
        activity: ActivityKey,
        conditions: Conditions | None = None,
        experience: Experience | None = None,
        owned_items: Annotated[
            list[OwnedItem] | None,
            Field(max_length=30, description="Equipment the user already has (category + optional product_id)"),
        ] = None,
        budget_eur_total: Annotated[float | None, Field(gt=0, le=50000)] = None,
        budget_by_category: Annotated[
            dict[str, float] | None, Field(description="Optional per-category budget, e.g. {'crampon': 150}")
        ] = None,
        candidates_per_category: Annotated[int, Field(ge=1, le=5)] = 3,
    ) -> EquipmentListResult:
        """Checklist by category for an activity: what the user already has (checked against mandatory
        requirements when a product_id is given), what is missing, and grounded candidates for missing items.
        The total is only given when every applicable price is known; there is no global budget optimization."""

        def body() -> EquipmentListResult:
            try:
                data = equipment.build_equipment_list(
                    activity=activity,
                    conditions=conditions,
                    experience=experience,
                    owned_items=[o.model_dump() for o in owned_items or []],
                    budget_eur_total=budget_eur_total,
                    budget_by_category=budget_by_category,
                    candidates_per_category=candidates_per_category,
                )
            except NotFoundError as exc:
                return EquipmentListResult(
                    status="not_found",
                    warnings=[str(exc)],
                    meta=_meta(),
                    data={"available_activities": equipment.list_activities()},
                )
            warnings = data.pop("warnings")
            partial = data["estimate"]["essential_top_candidates_total_eur"] is None or bool(data["missing_context"])
            return EquipmentListResult(
                status=_status(warnings, partial=partial), data=data, warnings=warnings, meta=_meta()
            )

        return _run(body, "build_equipment_list")

    return mcp
