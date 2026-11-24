"""Ingestion runs and the human review queue."""

from __future__ import annotations

import uuid

from django.db import models

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.sources.models import Source, SourceRecord


class IngestionRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        PARTIAL = "partial"  # finished with item-level errors
        FAILED = "failed"
        SKIPPED = "skipped"  # e.g. locked by a concurrent run

    class Trigger(models.TextChoices):
        CLI = "cli"
        SCHEDULED = "scheduled"
        IMPORT = "import"
        SEED = "seed"

    run_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    trigger = models.CharField(max_length=20, choices=Trigger.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    dry_run = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)
    actor = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.source.slug}#{self.run_id} {self.status}"


class ReviewItem(models.Model):
    class Kind(models.TextChoices):
        NEW_PRODUCT = "new_product"
        CRITICAL_CHANGE = "critical_change"
        CONFLICT = "conflict"
        AMBIGUOUS_MATCH = "ambiguous_match"
        INVALID_VALUE = "invalid_value"
        MISSING_AT_SOURCE = "missing_at_source"

    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        OBSOLETE = "obsolete"  # superseded by a newer observation

    kind = models.CharField(max_length=30, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name="review_items")
    source_record = models.ForeignKey(
        SourceRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="review_items"
    )
    ingestion_run = models.ForeignKey(
        IngestionRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="review_items"
    )
    field = models.CharField(max_length=80, blank=True)
    current_value = models.JSONField(null=True, blank=True)
    proposed_value = models.JSONField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True, help_text="Context needed to apply the decision")
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.CharField(max_length=120, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["status", "kind"])]

    def __str__(self) -> str:
        return f"#{self.pk} {self.kind} {self.status}"
