"""Activity profiles: validated schema, YAML loading and condition evaluation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mountain_intelligence.catalog.taxonomy import CATEGORIES, FieldType

from .models import ActivityProfile, ReviewStatus

Experience = Literal["beginner", "intermediate", "advanced"]


class Conditions(BaseModel):
    """User-provided context. Every field is optional: unknown is a valid answer."""

    model_config = ConfigDict(extra="forbid")
    min_temperature_c: float | None = Field(default=None, ge=-60, le=50)
    snow: bool | None = None
    ice: bool | None = None
    strong_wind: bool | None = None
    rain: bool | None = None
    duration_hours: float | None = Field(default=None, gt=0, le=24 * 30)


class AppliesIf(BaseModel):
    """All given clauses must hold. An unknown context value makes the clause 'unknown'."""

    model_config = ConfigDict(extra="forbid")
    snow: bool | None = None
    ice: bool | None = None
    snow_or_ice: bool | None = None
    rain: bool | None = None
    strong_wind: bool | None = None
    min_temperature_below_c: float | None = None
    duration_hours_at_least: float | None = None
    experience_in: list[Experience] | None = None

    def evaluate(self, c: Conditions, experience: Experience | None) -> bool | None:
        results: list[bool | None] = []
        for name in ("snow", "ice", "rain", "strong_wind"):
            expected = getattr(self, name)
            if expected is not None:
                actual = getattr(c, name)
                results.append(None if actual is None else actual == expected)
        if self.snow_or_ice is not None:
            if c.snow is True or c.ice is True:
                results.append(self.snow_or_ice)
            elif c.snow is False and c.ice is False:
                results.append(not self.snow_or_ice)
            else:
                results.append(None)
        if self.min_temperature_below_c is not None:
            t = c.min_temperature_c
            results.append(None if t is None else t < self.min_temperature_below_c)
        if self.duration_hours_at_least is not None:
            d = c.duration_hours
            results.append(None if d is None else d >= self.duration_hours_at_least)
        if self.experience_in is not None:
            results.append(None if experience is None else experience in self.experience_in)
        if any(r is False for r in results):
            return False
        if any(r is None for r in results):
            return None
        return True


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    op: Literal["eq", "in", "gte", "lte", "contains", "contains_any", "contains_all"]
    value: Any
    rationale: str = Field(min_length=5)
    applies_if: AppliesIf | None = None


class Requirement(Criterion):
    mandatory: bool = True


class Preference(Criterion):
    weight: float = Field(gt=0, le=1)


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    technical_fit: float = Field(default=0.45, ge=0, le=1)
    weight: float = Field(default=0.20, ge=0, le=1)
    price: float = Field(default=0.20, ge=0, le=1)
    preferences: float = Field(default=0.15, ge=0, le=1)


class CategoryNeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    importance: Literal["essential", "recommended", "optional"]
    rationale: str
    applies_if: AppliesIf | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    preferences: list[Preference] = Field(default_factory=list)
    min_coverage: float = Field(default=0.4, ge=0, le=1)
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)

    @model_validator(mode="after")
    def _fields_exist(self) -> CategoryNeed:
        definition = CATEGORIES.get(self.category)
        if definition is None:
            raise ValueError(f"unknown category {self.category!r}")
        for crit in [*self.requirements, *self.preferences]:
            spec = definition.field_map.get(crit.field)
            if spec is None:
                raise ValueError(f"{self.category}: unknown field {crit.field!r}")
            if crit.op not in spec.operators:
                raise ValueError(f"{self.category}.{crit.field}: operator {crit.op!r} not allowed")
            if spec.type is FieldType.TEXT:
                raise ValueError(f"{self.category}.{crit.field}: free-text fields cannot be criteria")
        return self


class ProfileDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description_en: str
    description_es: str
    context_questions: list[str] = Field(
        default_factory=list, description="Conditions keys that materially change the requirements"
    )
    needs: list[CategoryNeed]
    general_notes: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class ProfileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    version: int = Field(ge=1)
    name_en: str
    name_es: str
    review_status: Literal["pending_expert_review", "reviewed", "demo"]
    reviewed_by: str = ""
    reviewed_at: date | None = None
    definition: ProfileDefinition

    @model_validator(mode="after")
    def _review(self) -> ProfileEntry:
        if self.review_status == "reviewed" and (not self.reviewed_by or not self.reviewed_at):
            raise ValueError(f"{self.key}: reviewed profiles need reviewed_by and reviewed_at")
        return self


class ProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    profiles: list[ProfileEntry]


@transaction.atomic
def load_profiles(path: Path) -> dict[str, int]:
    data = ProfileFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    stats = {"created": 0, "unchanged": 0, "activated": 0}
    for entry in data.profiles:
        definition = entry.definition.model_dump(mode="json")
        existing = ActivityProfile.objects.filter(key=entry.key, version=entry.version).first()
        if existing is not None:
            if existing.definition != definition:
                raise ValueError(
                    f"{entry.key}@v{entry.version} already exists with a different definition; "
                    "bump the version instead of editing a published profile"
                )
            changed = (existing.review_status, existing.reviewed_by, existing.reviewed_at) != (
                entry.review_status,
                entry.reviewed_by,
                entry.reviewed_at,
            )
            if changed:
                existing.review_status, existing.reviewed_by = entry.review_status, entry.reviewed_by
                existing.reviewed_at = entry.reviewed_at
                existing.save()
            stats["unchanged"] += 1
            profile = existing
        else:
            profile = ActivityProfile(
                key=entry.key,
                version=entry.version,
                name_en=entry.name_en,
                name_es=entry.name_es,
                definition=definition,
                review_status=entry.review_status,
                reviewed_by=entry.reviewed_by,
                reviewed_at=entry.reviewed_at,
                is_active=False,
            )
            profile.save()
            stats["created"] += 1
        latest = ActivityProfile.objects.filter(key=entry.key).order_by("-version").first()
        if latest and latest.pk == profile.pk and not profile.is_active:
            ActivityProfile.objects.filter(key=entry.key, is_active=True).update(is_active=False)
            profile.is_active = True
            profile.save(update_fields=["is_active"])
            stats["activated"] += 1
    return stats


def active_profile(key: str) -> tuple[ActivityProfile, ProfileDefinition] | None:
    profile = ActivityProfile.objects.filter(key=key, is_active=True).first()
    if profile is None:
        return None
    return profile, ProfileDefinition.model_validate(profile.definition)


def profile_disclaimer(profile: ActivityProfile) -> str | None:
    if profile.review_status == ReviewStatus.PENDING_EXPERT_REVIEW:
        return (
            "This activity profile has NOT been reviewed by a qualified mountain professional; its "
            "requirements are orientative and must not be taken as a safety assessment."
        )
    if profile.review_status == ReviewStatus.DEMO:
        return "DEMO profile: illustrative only, not real advice."
    return None
