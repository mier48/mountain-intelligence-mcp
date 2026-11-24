"""Version-controlled registry of brands and sources (YAML), validated before it touches the DB."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mountain_intelligence.catalog.models import Brand
from mountain_intelligence.catalog.services import brand_slug, get_or_create_brand
from mountain_intelligence.catalog.taxonomy import CATEGORIES

from .models import Source

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


class BrandEntry(BaseModel):
    """A brand. Fields after `integration_status` document the evaluation and are not persisted."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    official_domain: str = ""
    domain_verified: bool = False
    specialties: list[str] = Field(default_factory=list)
    integration_status: Literal["planned", "evaluating", "active", "disabled"] = "planned"
    country: str = Field(default="", max_length=2, pattern=r"^([A-Z]{2})?$")
    verified_at: date | None = None
    robots: str = ""
    sitemaps: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _verified(self) -> BrandEntry:
        if self.domain_verified and (not self.official_domain or self.verified_at is None):
            raise ValueError(f"{self.name}: a verified domain needs official_domain and verified_at")
        return self

    @field_validator("official_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not _DOMAIN_RE.match(value):
            raise ValueError(f"invalid domain {value!r}")
        return value


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,118}$")
    name: str
    brand: str | None = None
    source_type: Literal[tuple(Source.SourceType.values)]  # type: ignore[valid-type]
    adapter: str
    base_url: str = ""
    allowed_domains: list[str] = Field(default_factory=list)
    access_status: Literal[tuple(Source.AccessStatus.values)] = "unverified"  # type: ignore[valid-type]
    access_notes: str = ""
    terms_reviewed_at: date | None = None
    enabled: bool = False
    sync_frequency_hours: int | None = Field(default=None, ge=1, le=24 * 90)
    auto_publish: bool = False
    store_raw_documents: bool = False
    categories: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list, description="Evidence URLs for the access decision")

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        out = [v.strip().lower() for v in values]
        for v in out:
            if not _DOMAIN_RE.match(v):
                raise ValueError(f"invalid domain {v!r}")
        return out

    @field_validator("categories")
    @classmethod
    def _categories(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown categories {unknown}")
        return values

    @model_validator(mode="after")
    def _consistency(self) -> SourceEntry:
        if self.enabled and self.access_status not in Source.ACCESS_ALLOWING_SYNC:
            raise ValueError(f"{self.slug}: cannot be enabled with access_status={self.access_status}")
        if self.adapter == "jsonld_product" and not self.allowed_domains:
            raise ValueError(f"{self.slug}: network adapters require allowed_domains")
        return self


class CandidateSource(BaseModel):
    """A data route under evaluation that has no adapter yet (documentation only, not persisted)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["distributor_affiliate_feed", "distributor_website", "industry_data_pool", "manufacturer_b2b"]
    url: str = ""
    provides: list[Literal["specifications", "prices", "availability", "identifiers", "sustainability"]]
    route: str
    status: Literal["to_evaluate", "requires_agreement", "out_of_scope", "not_viable"]
    notes: str = ""
    references: list[str] = Field(default_factory=list)


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    reviewed_at: date | None = None
    brands: list[BrandEntry] = Field(default_factory=list)
    sources: list[SourceEntry] = Field(default_factory=list)
    candidate_sources: list[CandidateSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Registry:
        for label, keys in (
            ("brand", [b.name.casefold() for b in self.brands]),
            ("source", [s.slug for s in self.sources]),
        ):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} entries: {dupes}")
        return self


def load_registry(path: Path) -> Registry:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Registry.model_validate(data)


@transaction.atomic
def apply_registry(registry: Registry) -> dict[str, int]:
    from mountain_intelligence.ingestion.adapters.base import available_adapters

    adapters = set(available_adapters())
    stats = {"brands": 0, "sources_created": 0, "sources_updated": 0}
    for entry in registry.brands:
        brand = get_or_create_brand(entry.name)
        brand.official_domain = entry.official_domain
        brand.integration_status = entry.integration_status
        brand.save()
        stats["brands"] += 1
    for entry in registry.sources:
        if entry.adapter not in adapters:
            raise ValueError(f"{entry.slug}: unknown adapter {entry.adapter!r}")
        brand = Brand.objects.filter(slug=brand_slug(entry.brand)).first() if entry.brand else None
        if entry.brand and brand is None:
            raise ValueError(f"{entry.slug}: brand {entry.brand!r} is not declared in the registry")
        values = entry.model_dump(exclude={"slug", "brand", "references"})
        values["brand"] = brand
        if entry.references:
            values["access_notes"] = (entry.access_notes + "\nReferences: " + " ; ".join(entry.references)).strip()
        _, created = Source.objects.update_or_create(slug=entry.slug, defaults=values)
        stats["sources_created" if created else "sources_updated"] += 1
    return stats
