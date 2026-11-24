"""Catalog domain primitives shared by ingestion, CLI and query services."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import Brand, Category, DataStatus, Product
from .normalization import fold
from .taxonomy import CATEGORIES, CategoryDefinition, FieldType, get_category


def sync_categories(using: str = "default") -> None:
    for definition in CATEGORIES.values():
        Category.objects.using(using).update_or_create(
            slug=definition.slug,
            defaults={
                "name_en": definition.name_en,
                "name_es": definition.name_es,
                "family": definition.family,
                "schema_version": definition.schema_version,
                "technical": definition.technical,
            },
        )


def brand_slug(name: str) -> str:
    return slugify(fold(name))[:120]


def get_or_create_brand(name: str, *, official_domain: str = "", is_synthetic: bool = False) -> Brand:
    slug = brand_slug(name)
    brand, created = Brand.objects.get_or_create(
        slug=slug, defaults={"name": name.strip(), "official_domain": official_domain, "is_synthetic": is_synthetic}
    )
    if not created and official_domain and not brand.official_domain:
        brand.official_domain = official_domain
        brand.save(update_fields=["official_domain", "updated_at"])
    return brand


def make_public_id(brand: Brand, name: str, generation: str = "") -> str:
    base = slugify(fold(f"{brand.slug} {name} {generation}".strip()))[:180] or "product"
    candidate, n = base, 2
    while Product.objects.filter(public_id=candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(fold(text))


def build_search_document(product: Product) -> str:
    """Accent-folded bag of words in both languages for PostgreSQL full-text + trigram search."""
    definition = get_category(product.category.slug)
    parts: list[str] = [
        product.brand.name,
        product.name,
        product.generation,
        product.mpn,
        *product.aliases,
        *definition.search_terms(),
        # Manufacturer metadata makes every product findable, including `uncategorized` ones.
        product.source_category,
        product.product_type,
        *product.tags[:60],
    ]
    for attr in (product.source_attributes or [])[:60]:
        if isinstance(attr, dict):
            parts.extend(str(attr.get(k) or "")[:120] for k in ("name", "value"))
    specs = product.specifications or {}
    for spec in definition.fields:
        value = specs.get(spec.name)
        if spec.type in (FieldType.ENUM, FieldType.TEXT) and isinstance(value, str):
            parts.append(value.replace("_", " "))
        elif spec.type in (FieldType.ENUM_LIST, FieldType.TEXT_LIST) and isinstance(value, list):
            parts.extend(str(v).replace("_", " ") for v in value)
    for variant in product.variants.all() if product.pk else ():
        parts.extend([variant.sku, variant.mpn, variant.gtin])
    return " ".join(dict.fromkeys(t for part in parts if part for t in tokens(str(part))))


def refresh_derived_and_search(product: Product, *, save: bool = True) -> None:
    definition = get_category(product.category.slug)
    product.derived_specifications = definition.compute_derived(product.specifications or {})
    product.spec_schema_version = definition.schema_version
    product.search_document = build_search_document(product)
    if save:
        product.save(update_fields=["derived_specifications", "spec_schema_version", "search_document", "updated_at"])


def visible_products(include_synthetic: bool | None = None) -> QuerySet[Product]:
    """Products that MCP query tools may expose."""
    if include_synthetic is None:
        include_synthetic = settings.MI_INCLUDE_SYNTHETIC_DATA
    qs = Product.objects.filter(publication_status=Product.PublicationStatus.PUBLISHED)
    if not include_synthetic:
        qs = qs.exclude(data_status=DataStatus.SYNTHETIC_FIXTURE)
    return qs.select_related("brand", "category")


def freshness(product: Product) -> str:
    if product.last_verified_at is None:
        return "unknown"
    if product.last_verified_at < timezone.now() - timedelta(days=settings.MI_STALE_AFTER_DAYS):
        return "stale"
    return "fresh"


def coverage(product: Product, definition: CategoryDefinition | None = None) -> float:
    """Share of the category's key technical fields with a known value (0 without technical schema)."""
    definition = definition or get_category(product.category.slug)
    if not definition.technical:
        return 0.0
    keys = definition.key_fields
    if not keys:
        return 1.0
    specs = product.specifications or {}
    known = sum(1 for f in keys if specs.get(f.name) is not None)
    return round(known / len(keys), 2)


def spec_value(product: Product, field: str) -> Any:
    if field in (product.specifications or {}):
        return product.specifications[field]
    return (product.derived_specifications or {}).get(field)


def find_by_identifier(identifier: str) -> Q:
    ident = identifier.strip()
    return (
        Q(public_id=ident.lower())
        | Q(mpn__iexact=ident)
        | Q(variants__sku__iexact=ident)
        | Q(variants__mpn__iexact=ident)
        | Q(variants__gtin=ident)
    )
