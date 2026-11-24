"""Versioned activity profiles and compatibility rules (loaded from version-controlled YAML)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from mountain_intelligence.catalog.models import DataStatus, Product


class ReviewStatus(models.TextChoices):
    PENDING_EXPERT_REVIEW = "pending_expert_review", "Authored but not reviewed by a qualified expert"
    REVIEWED = "reviewed", "Reviewed by a qualified person (see reviewed_by)"
    DEMO = "demo", "Demo/synthetic, never for real advice"


class ActivityProfile(models.Model):
    key = models.SlugField(max_length=80)
    version = models.PositiveIntegerField()
    name_en = models.CharField(max_length=150)
    name_es = models.CharField(max_length=150)
    definition = models.JSONField(help_text="Validated by recommendations.profiles.ProfileDefinition")
    review_status = models.CharField(max_length=30, choices=ReviewStatus.choices)
    reviewed_by = models.CharField(max_length=120, blank=True)
    reviewed_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["key", "version"], name="unique_profile_version"),
            models.UniqueConstraint(fields=["key"], condition=Q(is_active=True), name="one_active_profile_version"),
        ]

    def __str__(self) -> str:
        return f"{self.key}@v{self.version}"


class CompatibilityRule(models.Model):
    class Status(models.TextChoices):
        PUBLISHED = "published"
        DRAFT = "draft"
        DEMO = "demo"
        RETIRED = "retired"

    key = models.SlugField(max_length=80)
    version = models.PositiveIntegerField()
    relation = models.SlugField(max_length=60, help_text="e.g. boot_crampon")
    source_category = models.SlugField(max_length=60)
    target_category = models.SlugField(max_length=60)
    evaluator = models.SlugField(max_length=80, help_text="Registered evaluator implementing the rule")
    parameters = models.JSONField(default=dict, blank=True)
    description = models.TextField()
    references = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["relation", "key", "-version"]
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="unique_rule_version")]

    def __str__(self) -> str:
        return f"{self.key}@v{self.version} ({self.status})"


class CompatibilityAssertion(models.Model):
    """Pair-level statement documented by a manufacturer (e.g. a fit list for a specific boot)."""

    class Verdict(models.TextChoices):
        COMPATIBLE = "compatible"
        INCOMPATIBLE = "incompatible"
        CONDITIONAL = "conditional"

    relation = models.SlugField(max_length=60)
    product_a = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="assertions_as_a")
    product_b = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="assertions_as_b")
    verdict = models.CharField(max_length=20, choices=Verdict.choices)
    conditions = models.TextField(blank=True)
    declared_by = models.CharField(max_length=200, help_text="Who declares it (e.g. crampon manufacturer)")
    source_url = models.URLField(max_length=1000, blank=True)
    observed_at = models.DateTimeField()
    data_status = models.CharField(max_length=30, choices=DataStatus.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["relation", "product_a_id", "product_b_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["relation", "product_a", "product_b", "declared_by"], name="unique_assertion"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.relation}: {self.product_a_id}/{self.product_b_id} {self.verdict}"
