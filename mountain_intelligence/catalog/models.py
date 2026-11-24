"""Canonical catalog: brands, categories, products and commercial variants."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models import Q
from django.utils import timezone


class DataStatus(models.TextChoices):
    SOURCE_EXTRACTED = "source_extracted", "Extracted from a registered source"
    MANUAL_ENTRY = "manual_entry", "Authorized manual/structured import"
    SYNTHETIC_FIXTURE = "synthetic_fixture", "Synthetic fixture (demo/test only)"


class Brand(models.Model):
    class IntegrationStatus(models.TextChoices):
        PLANNED = "planned"
        EVALUATING = "evaluating"
        ACTIVE = "active"
        DISABLED = "disabled"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    official_domain = models.CharField(max_length=255, blank=True)
    integration_status = models.CharField(
        max_length=20, choices=IntegrationStatus.choices, default=IntegrationStatus.PLANNED
    )
    is_synthetic = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Category(models.Model):
    """Mirror of `taxonomy.CATEGORIES`, kept in sync automatically after `migrate`."""

    slug = models.SlugField(max_length=60, unique=True)
    name_en = models.CharField(max_length=120)
    name_es = models.CharField(max_length=120)
    family = models.CharField(max_length=60)
    schema_version = models.PositiveIntegerField(default=1)
    technical = models.BooleanField(
        default=True, help_text="False for the `uncategorized` fallback (no technical specification schema)"
    )

    class Meta:
        ordering = ["slug"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.slug


class Product(models.Model):
    """Canonical technical model (shared specifications across sizes/colours)."""

    class PublicationStatus(models.TextChoices):
        DRAFT = "draft"
        IN_REVIEW = "in_review"
        PUBLISHED = "published"
        WITHDRAWN = "withdrawn"  # removed from the catalog (not "out of stock")

    class SourceStatus(models.TextChoices):
        OK = "ok"
        MISSING_AT_SOURCE = "missing_at_source"
        FETCH_ERROR = "fetch_error"

    class CategoryBasis(models.TextChoices):
        UNKNOWN = "", "Not recorded (products created before classification tracking)"
        EXPLICIT = "explicit", "Category declared by an authorized structured source"
        RULE = "rule", "Matched a source classification rule"
        DEFAULT = "default", "Source default category"
        FALLBACK = "fallback", "No rule matched: uncategorized fallback"
        REVIEW = "review", "Set by a reviewer"

    public_id = models.SlugField(max_length=200, unique=True, help_text="Stable identifier exposed over MCP")
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=200)
    generation = models.CharField(max_length=60, blank=True, help_text="Model generation/version label")
    model_year = models.PositiveSmallIntegerField(null=True, blank=True)
    mpn = models.CharField(max_length=80, blank=True, help_text="Manufacturer part number of the model")
    aliases = ArrayField(models.CharField(max_length=200), default=list, blank=True)
    summary = models.CharField(max_length=500, blank=True, help_text="Short neutral summary (no copied marketing)")
    specifications = models.JSONField(default=dict, blank=True)
    derived_specifications = models.JSONField(default=dict, blank=True)
    spec_schema_version = models.PositiveIntegerField(default=1)
    publication_status = models.CharField(
        max_length=20, choices=PublicationStatus.choices, default=PublicationStatus.DRAFT
    )
    data_status = models.CharField(max_length=30, choices=DataStatus.choices)
    source_status = models.CharField(max_length=30, choices=SourceStatus.choices, default=SourceStatus.OK)
    source_status_since = models.DateTimeField(null=True, blank=True)
    pending_review_fields = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    category_basis = models.CharField(max_length=20, choices=CategoryBasis.choices, blank=True, default="")
    # Manufacturer catalog metadata, preserved as published by the source (not normalized). The
    # technical `specifications` above only ever hold schema-validated values.
    source_category = models.CharField(max_length=300, blank=True, help_text="Manufacturer's own category/path")
    product_type = models.CharField(max_length=120, blank=True, help_text="Manufacturer's product type")
    tags = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    collections = ArrayField(
        models.CharField(max_length=80), default=list, blank=True, help_text="Manufacturer collections (handles)"
    )
    external_ids = models.JSONField(default=dict, blank=True, help_text="Identifiers at the source (e.g. Shopify)")
    images = models.JSONField(default=list, blank=True, help_text="[{url, alt, position, variant_skus}]")
    source_attributes = models.JSONField(
        default=list, blank=True, help_text="Original declared characteristics [{name, value, unit, locator, source}]"
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    search_document = models.TextField(blank=True)
    search_vector = models.GeneratedField(
        expression=SearchVector("search_document", config="simple"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["public_id"]
        indexes = [
            GinIndex(fields=["search_vector"], name="product_search_vector_gin"),
            GinIndex(fields=["search_document"], opclasses=["gin_trgm_ops"], name="product_search_trgm"),
            GinIndex(fields=["specifications"], opclasses=["jsonb_path_ops"], name="product_specs_gin"),
            models.Index(fields=["category", "publication_status", "data_status"], name="product_scope_idx"),
            GinIndex(fields=["tags"], name="product_tags_gin"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["brand", "mpn"],
                condition=~Q(mpn=""),
                name="unique_brand_mpn",
            ),
            models.UniqueConstraint(
                fields=["brand", "category", "name", "generation"],
                name="unique_brand_model_generation",
            ),
        ]

    def __str__(self) -> str:
        return self.public_id

    @property
    def is_synthetic(self) -> bool:
        return self.data_status == DataStatus.SYNTHETIC_FIXTURE

    def mark_published(self) -> None:
        self.publication_status = self.PublicationStatus.PUBLISHED
        self.published_at = self.published_at or timezone.now()


class ProductVariant(models.Model):
    """Commercial variant (size/colour/SKU). Overrides only where specs genuinely differ."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=80, blank=True)
    mpn = models.CharField(max_length=80, blank=True)
    gtin = models.CharField(max_length=14, blank=True)
    size_label = models.CharField(max_length=40, blank=True)
    size_eu = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    color = models.CharField(max_length=80, blank=True)
    external_id = models.CharField(max_length=100, blank=True, help_text="Variant id at the source")
    options = models.JSONField(default=dict, blank=True, help_text="Original option name -> value")
    image_url = models.URLField(max_length=1000, blank=True)
    spec_overrides = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product_id", "size_eu", "size_label", "color", "id"]
        constraints = [
            models.UniqueConstraint(fields=["gtin"], condition=~Q(gtin=""), name="unique_variant_gtin"),
            models.UniqueConstraint(fields=["product", "sku"], condition=~Q(sku=""), name="unique_product_sku"),
        ]
        indexes = [models.Index(fields=["sku"]), models.Index(fields=["mpn"])]

    def __str__(self) -> str:
        return f"{self.product.public_id} [{self.sku or self.gtin or self.pk}]"
