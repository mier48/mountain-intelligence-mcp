"""Read-only catalog queries: structured search, product detail, comparison and brand listing.

All inputs are validated against the category taxonomy; no client-provided ORM lookups or
field names reach the database without passing through an allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Literal

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramWordSimilarity
from django.db.models import Case, Count, F, FloatField, IntegerField, Q, QuerySet, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from pydantic import BaseModel, ConfigDict, Field

from mountain_intelligence.offers.services import availability_summary, comparable_price_eur, current_offers
from mountain_intelligence.sources.models import SourceRecord, SpecificationEvidence

from .models import Brand, Product, ProductVariant
from .normalization import FieldIssue, fold, normalize_value
from .services import coverage, find_by_identifier, freshness, spec_value, tokens, visible_products
from .synonyms import STOPWORDS, expand
from .taxonomy import CATEGORIES, UNCATEGORIZED_SLUG, CategoryDefinition, FieldSpec, FieldType, get_category
from .units import UnitError

MAX_LIMIT = 25
MAX_OFFSET = 500
MAX_FILTERS = 12


class QueryValidationError(ValueError):
    """Client input that cannot be executed (unknown field, bad operator, bad value...)."""


class NotFoundError(LookupError):
    pass


class SpecFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(max_length=80, description="Specification field name (see list_categories)")
    op: Literal["eq", "in", "gte", "lte", "contains", "contains_any", "contains_all"]
    value: bool | int | float | str | list[str | int | float] = Field(
        description="Value in the field's canonical unit (e.g. grams for *_g, litres for *_l)"
    )


# ------------------------------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------------------------------


def _normalize_filter_value(spec: FieldSpec, value: Any) -> Any:
    try:
        return normalize_value(spec, value, assumed_unit=spec.unit).normalized
    except (UnitError, ValueError, TypeError) as exc:
        raise QueryValidationError(f"invalid value for filter {spec.name!r}: {exc}") from exc


def apply_filters(
    qs: QuerySet[Product], definition: CategoryDefinition, filters: list[SpecFilter]
) -> QuerySet[Product]:
    if len(filters) > MAX_FILTERS:
        raise QueryValidationError(f"at most {MAX_FILTERS} filters are allowed")
    for i, flt in enumerate(filters):
        spec = definition.field_map.get(flt.field)
        if spec is None or not spec.filterable:
            allowed = [f.name for f in definition.fields if f.filterable]
            raise QueryValidationError(
                f"field {flt.field!r} is not filterable for category {definition.slug!r}; allowed: {allowed}"
            )
        if flt.op not in spec.operators:
            raise QueryValidationError(
                f"operator {flt.op!r} not supported for {spec.name!r} ({spec.type.value}); "
                f"allowed: {list(spec.operators)}"
            )
        column = "derived_specifications" if spec.derived else "specifications"
        if spec.type in (FieldType.NUMBER, FieldType.INTEGER):
            alias = f"_flt_{i}"
            qs = qs.annotate(**{alias: Cast(KeyTextTransform(spec.name, column), FloatField())})
            if flt.op == "in":
                if not isinstance(flt.value, list) or not flt.value:
                    raise QueryValidationError("'in' expects a non-empty list")
                values = [float(_normalize_filter_value(spec, v)) for v in flt.value[:50]]
                qs = qs.filter(**{f"{alias}__in": values})
            else:
                number = float(_normalize_filter_value(spec, flt.value))
                lookup = {"eq": "exact", "gte": "gte", "lte": "lte"}[flt.op]
                qs = qs.filter(**{f"{alias}__{lookup}": number})
        elif spec.type is FieldType.BOOLEAN:
            qs = qs.filter(**{f"{column}__{spec.name}": bool(_normalize_filter_value(spec, flt.value))})
        elif spec.type is FieldType.ENUM:
            if flt.op == "in":
                if not isinstance(flt.value, list) or not flt.value:
                    raise QueryValidationError("'in' expects a non-empty list")
                values = [_normalize_filter_value(spec, v) for v in flt.value[:50]]
                qs = qs.filter(**{f"{column}__{spec.name}__in": values})
            else:
                qs = qs.filter(**{f"{column}__{spec.name}": _normalize_filter_value(spec, flt.value)})
        elif spec.type is FieldType.ENUM_LIST:
            raw = flt.value if isinstance(flt.value, list) else [flt.value]
            values = _normalize_filter_value(spec, raw)
            if not values:
                raise QueryValidationError("empty list filter")
            if flt.op == "contains_any":
                any_q = Q()
                for v in values:
                    any_q |= Q(**{f"{column}__{spec.name}__contains": [v]})
                qs = qs.filter(any_q)
            else:  # contains / contains_all
                qs = qs.filter(**{f"{column}__{spec.name}__contains": values})
        elif spec.type is FieldType.TEXT_LIST:
            value = _normalize_filter_value(spec, [flt.value] if not isinstance(flt.value, list) else flt.value)
            qs = qs.filter(**{f"{column}__{spec.name}__contains": value})
        else:
            qs = qs.filter(**{f"{column}__{spec.name}__iexact": str(flt.value)})
    return qs


# ------------------------------------------------------------------------------------------
# Search
# ------------------------------------------------------------------------------------------


def _tsquery(query: str, *, mode: Literal["and", "or"]) -> str | None:
    groups: list[str] = []
    for token in tokens(query):
        if token in STOPWORDS or len(token) < 2:
            continue
        alternatives = []
        for term in expand(token):
            stem = term[:-1] if term.endswith("s") and len(term) > 4 else term
            alternatives.append(f"{stem}:*")
        groups.append("(" + " | ".join(dict.fromkeys(alternatives)) + ")")
    if not groups:
        return None
    return (" & " if mode == "and" else " | ").join(groups[:12])


def _cursor_key(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def encode_cursor(offset: int, params: dict[str, Any]) -> str:
    raw = json.dumps({"o": offset, "k": _cursor_key(params)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, params: dict[str, Any]) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        offset = int(data["o"])
    except (ValueError, KeyError, TypeError) as exc:
        raise QueryValidationError("invalid cursor") from exc
    if data.get("k") != _cursor_key(params):
        raise QueryValidationError("cursor does not belong to this query; repeat the search without cursor")
    if not 0 <= offset <= MAX_OFFSET:
        raise QueryValidationError("cursor out of range")
    return offset


def resolve_brands(brands: list[str]) -> list[str]:
    from .services import brand_slug

    return [brand_slug(b) for b in brands[:20] if b.strip()]


def list_brands(*, category: str | None = None, include_synthetic: bool | None = None) -> list[dict[str, Any]]:
    """Registered brands with their published product counts per category, ordered by name.

    Without `category` every brand is listed, including those with no published products yet (the
    catalog scope is part of the answer). With `category` only brands that have products there.
    """
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    if category and category not in CATEGORIES:
        raise QueryValidationError(f"unknown category {category!r}")
    counts: dict[int, dict[str, int]] = {}
    rows = visible_products(include_synthetic).order_by().values("brand_id", "category__slug").annotate(n=Count("pk"))
    for row in rows:
        counts.setdefault(row["brand_id"], {})[row["category__slug"]] = row["n"]
    brands = Brand.objects.all() if include_synthetic else Brand.objects.filter(is_synthetic=False)
    items = []
    for brand in brands.order_by("name"):
        by_category = counts.get(brand.pk, {})
        if category and category not in by_category:
            continue
        items.append(
            {
                "name": brand.name,
                "slug": brand.slug,
                "official_domain": brand.official_domain or None,
                "published_products": by_category[category] if category else sum(by_category.values()),
                "categories": sorted(by_category),
                "synthetic": brand.is_synthetic,
            }
        )
    return items


def search_products(
    *,
    query: str = "",
    category: str | None = None,
    brands: list[str] | None = None,
    filters: list[SpecFilter] | None = None,
    limit: int = 10,
    cursor: str | None = None,
    include_synthetic: bool | None = None,
    classification: Literal["any", "technical", "uncategorized"] = "any",
) -> dict[str, Any]:
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    limit = max(1, min(limit, MAX_LIMIT))
    filters = filters or []
    warnings: list[str] = []
    if filters and not category:
        raise QueryValidationError("filters require a category (fields are category-specific)")
    if category and category not in CATEGORIES:
        raise QueryValidationError(f"unknown category {category!r}")
    definition = get_category(category) if category else None
    if definition is not None and not definition.technical and filters:
        raise QueryValidationError(
            "uncategorized products have no technical schema: specification filters are not available; "
            "use the text query (manufacturer category, type, tags, declared attributes)"
        )

    qs = visible_products(include_synthetic)
    if classification == "technical":
        qs = qs.exclude(category__slug=UNCATEGORIZED_SLUG)
    elif classification == "uncategorized":
        qs = qs.filter(category__slug=UNCATEGORIZED_SLUG)
    if definition:
        qs = qs.filter(category__slug=definition.slug)
        qs = apply_filters(qs, definition, filters)
    if brands:
        qs = qs.filter(brand__slug__in=resolve_brands(brands))

    query = " ".join(query.split())[:200]
    order: list[Any] = []
    if query:
        ident_ids = list(qs.filter(find_by_identifier(query)).values_list("id", flat=True).distinct()[:20])
        tsq = _tsquery(query, mode="and")
        text_q = Q(id__in=ident_ids)
        if tsq:
            text_q |= Q(search_vector=SearchQuery(tsq, search_type="raw", config="simple"))
        matched = qs.filter(text_q)
        if tsq and not matched.exists():
            relaxed = _tsquery(query, mode="or")
            folded = fold(query)
            matched = qs.annotate(_tw=TrigramWordSimilarity(folded, "search_document")).filter(
                Q(search_vector=SearchQuery(relaxed, search_type="raw", config="simple"))
                | Q(_tw__gte=0.5)
                | Q(id__in=ident_ids)
            )
            warnings.append("no product matched every search term; showing partial text matches")
        qs = matched
        rank_query = SearchQuery(tsq or "", search_type="raw", config="simple") if tsq else None
        qs = qs.annotate(
            _ident=Case(When(id__in=ident_ids, then=Value(1)), default=Value(0), output_field=IntegerField()),
            _rank=SearchRank(F("search_vector"), rank_query) if rank_query else Value(0.0, FloatField()),
        )
        order = ["-_ident", "-_rank"]
    params = {
        "q": query,
        "c": category,
        "b": sorted(brands or []),
        "f": [f.model_dump() for f in filters],
        "s": include_synthetic,
        "t": classification,
    }
    offset = decode_cursor(cursor, params)
    qs = qs.distinct().order_by(*order, "brand__name", "name", "public_id")
    total = qs.count()
    page = list(qs[offset : offset + limit])
    next_cursor = (
        encode_cursor(offset + limit, params) if offset + limit < total and offset + limit <= MAX_OFFSET else None
    )
    items = [product_summary(p, include_synthetic=include_synthetic) for p in page]
    if any(i["data_status"] == "synthetic_fixture" for i in items):
        warnings.append("results include SYNTHETIC demo fixtures, not real products")
    if any(not i["technical_classification"] for i in items):
        warnings.append(
            "results include UNCATEGORIZED products: catalog data only (no validated technical specifications)"
        )
    return {"items": items, "total": total, "next_cursor": next_cursor, "warnings": warnings}


def _key_specs(product: Product, definition: CategoryDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for spec in definition.fields:
        value = spec_value(product, spec.name)
        if value is not None and spec.type is not FieldType.TEXT:
            out[spec.name] = value
    return out


def _primary_source_url(product: Product) -> str | None:
    record = SourceRecord.objects.filter(product=product).exclude(canonical_url="").order_by("-last_seen_at").first()
    return record.canonical_url if record else None


def product_warnings(product: Product) -> list[str]:
    warnings = []
    if product.is_synthetic:
        warnings.append("SYNTHETIC demo fixture: not a real product")
    if product.category.slug == UNCATEGORIZED_SLUG:
        warnings.append(
            "not technically classified (uncategorized): no validated specifications; excluded from "
            "recommendations, comparisons and compatibility checks"
        )
    if product.pending_review_fields:
        warnings.append(f"pending review for fields {product.pending_review_fields}: current values may be outdated")
    if product.source_status != Product.SourceStatus.OK:
        since = product.source_status_since.date().isoformat() if product.source_status_since else "unknown date"
        warnings.append(f"source status '{product.source_status}' since {since}; data kept from last verification")
    if freshness(product) == "stale":
        warnings.append("data not re-verified recently (stale)")
    return warnings


def product_summary(product: Product, *, include_synthetic: bool) -> dict[str, Any]:
    definition = get_category(product.category.slug)
    price = comparable_price_eur(product, include_synthetic=include_synthetic)
    first_image = next((i.get("url") for i in product.images or [] if isinstance(i, dict) and i.get("url")), None)
    return {
        "product_id": product.public_id,
        "brand": product.brand.name,
        "name": product.name,
        "generation": product.generation or None,
        "category": product.category.slug,
        "technical_classification": definition.technical,
        "source_category": product.source_category or None,
        "product_type": product.product_type or None,
        "image_url": first_image,
        "key_specs": _key_specs(product, definition),
        "coverage": coverage(product, definition),
        "price_eur": float(price.amount) if price else None,
        "data_status": product.data_status,
        "freshness": freshness(product),
        "last_verified_at": product.last_verified_at.isoformat() if product.last_verified_at else None,
        "source_url": _primary_source_url(product),
        "warnings": product_warnings(product),
    }


# ------------------------------------------------------------------------------------------
# Detail
# ------------------------------------------------------------------------------------------


def get_visible_product(product_id: str, include_synthetic: bool | None = None) -> Product:
    product = visible_products(include_synthetic).filter(public_id=product_id.strip().lower()).first()
    if product is None:
        raise NotFoundError(f"product {product_id!r} not found in the published catalog")
    return product


def _evidence_list(product: Product, field: str, limit: int = 3) -> list[dict[str, Any]]:
    rows = SpecificationEvidence.objects.filter(product=product, field=field, is_current=True).select_related("source")[
        :limit
    ]
    return [
        {
            "source": e.source.name if e.source else None,
            "source_url": e.source_url or None,
            "observed_at": e.observed_at.isoformat(),
            "status": e.status,
            "original_value": e.value_original if isinstance(e.value_original, (str, int, float, bool, list)) else None,
            "original_unit": e.unit_original or None,
            "locator": e.locator or None,
        }
        for e in rows
    ]


def product_detail(
    product_id: str, *, variant: str | None = None, include_evidence: bool = True, include_synthetic: bool | None = None
) -> dict[str, Any]:
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    product = get_visible_product(product_id, include_synthetic)
    definition = get_category(product.category.slug)
    selected: ProductVariant | None = None
    if variant:
        selected = product.variants.filter(Q(sku__iexact=variant) | Q(gtin=variant) | Q(mpn__iexact=variant)).first()
        if selected is None:
            raise NotFoundError(f"variant {variant!r} not found for {product.public_id}")
    overrides = selected.spec_overrides if selected else {}
    specs = []
    for spec in definition.fields:
        value = overrides.get(spec.name, spec_value(product, spec.name))
        row: dict[str, Any] = {"field": spec.name, "value": value, "unit": spec.unit, "known": value is not None}
        if spec.derived:
            row["derived"] = True
        elif include_evidence and value is not None:
            row["evidence"] = _evidence_list(product, spec.name)
        if spec.name in product.pending_review_fields:
            row["pending_review"] = True
        specs.append(row)
    variants = [
        {
            "sku": v.sku or None,
            "gtin": v.gtin or None,
            "size_label": v.size_label or None,
            "size_eu": float(v.size_eu) if v.size_eu is not None else None,
            "color": v.color or None,
            "options": v.options or None,
            "external_id": v.external_id or None,
            "image_url": v.image_url or None,
            "has_spec_overrides": bool(v.spec_overrides),
        }
        for v in product.variants.all()[:200]
    ]
    offers = [
        {
            "retailer": o.retailer,
            "price": float(o.price),
            "currency": o.currency,
            "availability": o.availability,
            "observed_at": o.observed_at.isoformat(),
            "url": o.url or None,
            "data_status": o.data_status,
            "variant_sku": o.variant.sku if o.variant_id and o.variant else None,
        }
        for o in current_offers(product, include_synthetic=include_synthetic)
    ]
    sources = [
        {
            "source": r.source.name,
            "url": r.canonical_url or None,
            "status": r.status,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in SourceRecord.objects.filter(product=product).select_related("source")[:10]
    ]
    warnings = product_warnings(product)
    if not offers:
        warnings.append("no recent price observation available")
    return {
        "product_id": product.public_id,
        "brand": product.brand.name,
        "name": product.name,
        "generation": product.generation or None,
        "model_year": product.model_year,
        "category": product.category.slug,
        "technical_classification": definition.technical,
        "category_basis": product.category_basis or None,
        "category_schema_version": product.spec_schema_version,
        "mpn": product.mpn or None,
        "aliases": product.aliases,
        "summary": product.summary or None,
        "selected_variant": variant if selected else None,
        "specifications": specs,
        "source_category": product.source_category or None,
        "product_type": product.product_type or None,
        "tags": product.tags,
        "collections": product.collections,
        "external_ids": product.external_ids or {},
        "images": (product.images or [])[:50],
        "source_attributes": [
            {k: a.get(k) for k in ("name", "value", "unit", "source")}
            for a in (product.source_attributes or [])[:150]
            if isinstance(a, dict)
        ],
        "coverage": coverage(product, definition),
        "variants": variants,
        "offers": offers,
        "availability": availability_summary(product, include_synthetic=include_synthetic),
        "sources": sources,
        "data_status": product.data_status,
        "publication_status": product.publication_status,
        "freshness": freshness(product),
        "last_verified_at": product.last_verified_at.isoformat() if product.last_verified_at else None,
        "warnings": warnings,
    }


# ------------------------------------------------------------------------------------------
# Comparison
# ------------------------------------------------------------------------------------------


def compare_products(product_ids: list[str], *, include_synthetic: bool | None = None) -> dict[str, Any]:
    include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA if include_synthetic is None else include_synthetic
    ids = list(dict.fromkeys(p.strip().lower() for p in product_ids))
    if not 2 <= len(ids) <= 5:
        raise QueryValidationError("compare_products needs between 2 and 5 distinct product ids")
    products = [get_visible_product(pid, include_synthetic) for pid in ids]
    definitions = [get_category(p.category.slug) for p in products]
    unclassified = [p.public_id for p, d in zip(products, definitions, strict=True) if not d.technical]
    if unclassified:
        raise QueryValidationError(
            f"{unclassified} have no technical classification (uncategorized): there are no validated "
            "specifications to compare; use get_product for their catalog data"
        )
    families = {d.family for d in definitions}
    if len(families) != 1:
        raise QueryValidationError(f"products belong to unrelated categories {sorted(d.slug for d in definitions)}")
    same_category = len({d.slug for d in definitions}) == 1
    limitations: list[str] = []
    if not same_category:
        limitations.append("different categories in the same family: only shared fields are compared")
    field_names = [f.name for f in definitions[0].fields if all(f.name in d.field_map for d in definitions[1:])]
    spec_of = definitions[0].field_map
    rows = []
    for name in field_names:
        spec = spec_of[name]
        values = [spec_value(p, name) for p in products]
        row: dict[str, Any] = {"field": name, "unit": spec.unit, "values": {}, "unknown_for": []}
        for p, v in zip(products, values, strict=True):
            row["values"][p.public_id] = v
            if v is None:
                row["unknown_for"].append(p.public_id)
        known = [(p.public_id, v) for p, v in zip(products, values, strict=True) if v is not None]
        if spec.type in (FieldType.NUMBER, FieldType.INTEGER) and len(known) >= 2:
            lo = min(known, key=lambda kv: kv[1])
            hi = max(known, key=lambda kv: kv[1])
            row["range"] = {"min": lo[1], "max": hi[1], "min_product": lo[0], "max_product": hi[0]}
            if spec.lower_is_lighter:
                row["note"] = f"lightest declared: {lo[0]}"
        distinct = {json.dumps(v, sort_keys=True) for _, v in known}
        row["differs"] = len(distinct) > 1 or (bool(known) and len(known) < len(products))
        rows.append(row)
    limitations.extend(_comparison_limitations(products, definitions[0]))
    unknown_cells = sum(len(r["unknown_for"]) for r in rows)
    return {
        "products": [product_summary(p, include_synthetic=include_synthetic) for p in products],
        "rows": rows,
        "limitations": limitations,
        "unknown_cells": unknown_cells,
    }


def _comparison_limitations(products: list[Product], definition: CategoryDefinition) -> list[str]:
    notes: list[str] = []
    bases = {p.public_id: (p.specifications or {}).get("weight_basis") for p in products}
    if "weight_basis" in definition.field_map:
        if any(b is None for b in bases.values()) and any((p.specifications or {}).get("weight_g") for p in products):
            missing = [pid for pid, b in bases.items() if b is None]
            notes.append(
                f"weight basis (per unit vs per pair) unknown for {missing}: raw weight_g is not comparable for them"
            )
        elif len({b for b in bases.values() if b}) > 1:
            notes.append(
                "declared weights use different bases (per unit vs per pair); compare the derived "
                "per-unit/per-pair field instead of weight_g"
            )
    sizes = {(p.specifications or {}).get("reference_size_eu") for p in products} - {None}
    if len(sizes) > 1:
        notes.append(f"weights declared for different reference sizes {sorted(sizes)}")
    if "breathability_ret" in definition.field_map:
        has_ret = [p.public_id for p in products if (p.specifications or {}).get("breathability_ret") is not None]
        has_mvtr = [p.public_id for p in products if (p.specifications or {}).get("breathability_mvtr") is not None]
        if has_ret and has_mvtr and set(has_ret) != set(has_mvtr):
            notes.append(
                "breathability is declared as RET for some products and MVTR for others; "
                "RET and MVTR are different magnitudes and cannot be compared"
            )
        methods = {(p.specifications or {}).get("breathability_test_method") for p in products} - {None}
        if len(methods) > 1:
            notes.append(f"breathability measured with different protocols {sorted(methods)}")
    return notes


def describe_issue(issue: FieldIssue) -> str:  # pragma: no cover - used by CLI output
    return f"{issue.field}: {issue.code} ({issue.message})"
