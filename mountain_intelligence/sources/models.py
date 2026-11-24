"""Sources, source records and per-field specification evidence (provenance)."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from mountain_intelligence.catalog.models import Brand, Product, ProductVariant


class Source(models.Model):
    class SourceType(models.TextChoices):
        MANUFACTURER_API = "manufacturer_api"
        MANUFACTURER_FEED = "manufacturer_feed"
        MANUFACTURER_WEBSITE = "manufacturer_website"  # sitemap + JSON-LD/HTML
        DISTRIBUTOR_FEED = "distributor_feed"
        STRUCTURED_FILE = "structured_file"  # authorized JSON/CSV delivered to the operator
        SYNTHETIC = "synthetic"

    class AccessStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Terms/permission not yet reviewed"
        AUTHORIZED = "authorized", "Explicit permission, licence or agreement"
        TERMS_PERMIT = "terms_permit", "Reviewed terms allow this use"
        RESTRICTED = "restricted", "Terms restrict or forbid this use"
        NOT_VIABLE = "not_viable", "Technically not viable (blocked, anti-bot, no data)"

    ACCESS_ALLOWING_SYNC = (AccessStatus.AUTHORIZED, AccessStatus.TERMS_PERMIT)

    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=200)
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, null=True, blank=True, related_name="sources")
    source_type = models.CharField(max_length=30, choices=SourceType.choices)
    adapter = models.CharField(max_length=60, help_text="Registered adapter key")
    base_url = models.URLField(blank=True)
    allowed_domains = ArrayField(models.CharField(max_length=255), default=list, blank=True)
    access_status = models.CharField(max_length=20, choices=AccessStatus.choices, default=AccessStatus.UNVERIFIED)
    access_notes = models.TextField(blank=True)
    terms_reviewed_at = models.DateField(null=True, blank=True)
    enabled = models.BooleanField(default=False)
    sync_frequency_hours = models.PositiveIntegerField(null=True, blank=True)
    auto_publish = models.BooleanField(default=False, help_text="Publish new products without review")
    store_raw_documents = models.BooleanField(default=False, help_text="Retention of raw documents (rights)")
    is_synthetic = models.BooleanField(default=False)
    config = models.JSONField(default=dict, blank=True, help_text="Non-sensitive adapter configuration")
    categories = ArrayField(
        models.CharField(max_length=60),
        default=list,
        blank=True,
        help_text="Allowed categories; empty = unrestricted (full manufacturer catalog, incl. uncategorized)",
    )
    sync_state = models.JSONField(
        default=dict, blank=True, help_text="Runtime batch-sync cursor (managed by ingestion, never by the registry)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]
        constraints = [
            models.CheckConstraint(
                condition=Q(enabled=False) | Q(access_status__in=["authorized", "terms_permit"]),
                name="enabled_source_requires_access",
            )
        ]

    def __str__(self) -> str:
        return self.slug

    @property
    def can_sync(self) -> bool:
        return self.enabled and self.access_status in self.ACCESS_ALLOWING_SYNC

    @property
    def unrestricted_categories(self) -> bool:
        """`categories: []` means the whole catalog is in scope (any category or `uncategorized`)."""
        return not self.categories

    def accepts_category(self, slug: str) -> bool:
        return self.unrestricted_categories or slug in self.categories


class SourceRecord(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        MISSING = "missing"  # not present in the latest complete discovery
        GONE = "gone"  # 404/410 at source
        ERROR = "error"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="records")
    external_id = models.CharField(max_length=300)
    canonical_url = models.URLField(max_length=1000, blank=True)
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_records"
    )
    content_hash = models.CharField(max_length=64, blank=True)
    etag = models.CharField(max_length=300, blank=True)
    last_modified = models.CharField(max_length=100, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    extracted_payload = models.JSONField(default=dict, blank=True, help_text="Validated extraction contract")
    raw_document_path = models.CharField(max_length=500, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_changed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="unique_source_record")]

    def __str__(self) -> str:
        return f"{self.source.slug}:{self.external_id}"


class SpecificationEvidence(models.Model):
    class Status(models.TextChoices):
        SOURCE_VERIFIED = "source_verified", "Observed at a registered source"
        MANUAL_IMPORT = "manual_import", "Authorized structured import"
        REVIEW_APPROVED = "review_approved", "Approved by a reviewer"
        PENDING_REVIEW = "pending_review", "Observed but awaiting review (not applied)"
        SUPERSEDED = "superseded", "Replaced by newer evidence"
        REJECTED = "rejected", "Rejected by a reviewer"
        SYNTHETIC_FIXTURE = "synthetic_fixture", "Synthetic fixture"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="evidence")
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, null=True, blank=True, related_name="evidence"
    )
    field = models.CharField(max_length=80)
    value_original = models.JSONField(null=True, blank=True)
    value_normalized = models.JSONField(null=True, blank=True)
    unit_original = models.CharField(max_length=40, blank=True)
    unit_normalized = models.CharField(max_length=40, blank=True)
    source = models.ForeignKey(Source, on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence")
    source_record = models.ForeignKey(
        SourceRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence"
    )
    source_url = models.URLField(max_length=1000, blank=True)
    locator = models.CharField(max_length=300, blank=True, help_text="JSON path / stable key in the source")
    excerpt = models.CharField(max_length=280, blank=True, help_text="Short supporting fragment, if authorized")
    note = models.CharField(max_length=300, blank=True)
    observed_at = models.DateTimeField()
    status = models.CharField(max_length=30, choices=Status.choices)
    is_current = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product_id", "field", "-observed_at"]
        indexes = [models.Index(fields=["product", "field", "is_current"], name="evidence_current_idx")]

    def __str__(self) -> str:
        return f"{self.product_id}.{self.field}={self.value_normalized!r} ({self.status})"
