"""Data contracts between adapters and the common normalization/persistence pipeline.

Adapters never touch canonical models: they only produce `ExtractedProduct` objects. Everything
inside them is untrusted external data and is validated again downstream.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GTIN_RE = re.compile(r"^\d{8}$|^\d{12,14}$")
_HTTP_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)

CategoryBasis = Literal["explicit", "rule", "default", "fallback"]


def gtin_is_valid(code: str) -> bool:
    """GS1 check-digit validation for GTIN-8/12/13/14."""
    if not _GTIN_RE.match(code):
        return False
    digits = [int(d) for d in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FieldLocator(StrictModel):
    locator: str = Field(default="", max_length=300)
    excerpt: str = Field(default="", max_length=280)
    unit: str | None = Field(default=None, max_length=20)


def _http_url(value: str) -> str:
    if value and not _HTTP_URL_RE.match(value):
        raise ValueError("only absolute http(s) URLs are accepted")
    return value


class ExtractedImage(StrictModel):
    url: str = Field(max_length=1000)
    alt: str = Field(default="", max_length=200)
    position: int | None = Field(default=None, ge=0, le=1000)
    variant_skus: list[str] = Field(default_factory=list, max_length=100)

    _url = field_validator("url")(classmethod(lambda cls, v: _http_url(v)))


class SourceAttribute(StrictModel):
    """A characteristic exactly as the manufacturer declares it (not normalized, not validated).

    Kept for traceability and progressive enrichment. Only schema fields of a technical category are
    ever promoted to normalized `specifications`.
    """

    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    unit: str = Field(default="", max_length=20)
    locator: str = Field(default="", max_length=300)


class ExtractedVariant(StrictModel):
    external_id: str = Field(default="", max_length=100)
    sku: str = Field(default="", max_length=80)
    mpn: str = Field(default="", max_length=80)
    gtin: str = Field(default="", max_length=14)
    size_label: str = Field(default="", max_length=40)
    size_eu: Decimal | None = Field(default=None, ge=15, le=55)
    color: str = Field(default="", max_length=80)
    options: dict[str, str] = Field(default_factory=dict, max_length=10)
    image_url: str = Field(default="", max_length=1000)
    spec_overrides: dict[str, Any] = Field(default_factory=dict)

    _image = field_validator("image_url")(classmethod(lambda cls, v: _http_url(v)))

    @field_validator("gtin")
    @classmethod
    def _gtin(cls, value: str) -> str:
        if value and not gtin_is_valid(value):
            raise ValueError("invalid GTIN check digit or length")
        return value


class ExtractedOffer(StrictModel):
    retailer: str = Field(max_length=120)
    url: str = Field(default="", max_length=1000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price: Decimal = Field(gt=0, lt=100000)
    availability: Literal["in_stock", "out_of_stock", "preorder", "unknown"] = "unknown"
    observed_at: datetime | None = None
    variant_sku: str = Field(default="", max_length=80)


class ExtractedProduct(StrictModel):
    external_id: str = Field(min_length=1, max_length=300)
    canonical_url: str = Field(default="", max_length=1000)
    brand: str = Field(min_length=1, max_length=120)
    brand_domain: str = Field(default="", max_length=255)
    name: str = Field(min_length=1, max_length=200)
    generation: str = Field(default="", max_length=60)
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    category: str = Field(min_length=1, max_length=60)
    # How `category` was decided; `fallback` means `uncategorized` because nothing matched.
    category_basis: CategoryBasis = "explicit"
    mpn: str = Field(default="", max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=500)
    specifications: dict[str, Any] = Field(default_factory=dict)
    # True when keys carry their unit (weight_g, capacity_l) under an explicit contract.
    units_from_field_names: bool = False
    locators: dict[str, FieldLocator] = Field(default_factory=dict)
    variants: list[ExtractedVariant] = Field(default_factory=list, max_length=500)
    offers: list[ExtractedOffer] = Field(default_factory=list, max_length=500)
    # Manufacturer catalog metadata, preserved as published (never used as technical facts).
    source_category: str = Field(default="", max_length=300)
    product_type: str = Field(default="", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=100)
    collections: list[str] = Field(default_factory=list, max_length=50)
    external_ids: dict[str, str] = Field(default_factory=dict, max_length=20)
    images: list[ExtractedImage] = Field(default_factory=list, max_length=50)
    source_attributes: list[SourceAttribute] = Field(default_factory=list, max_length=150)

    _canonical = field_validator("canonical_url")(classmethod(lambda cls, v: _http_url(v)))

    @field_validator("tags", "collections")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(t.split())[:80] for t in value]
        return list(dict.fromkeys(t for t in cleaned if t))

    @field_validator("external_ids")
    @classmethod
    def _external_ids(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 60 or len(v) > 300 for k, v in value.items()):
            raise ValueError("external id keys are limited to 60 chars and values to 300")
        return value

    @field_validator("specifications")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 80:
            raise ValueError("too many specification fields")
        return value


class DiscoveredItem(StrictModel):
    external_id: str
    url: str = ""
    lastmod: str = Field(default="", max_length=40)  # sitemap <lastmod>, informational
    inline_payload: dict[str, Any] | None = None  # structured imports carry data directly


class FetchedDocument(StrictModel):
    item: DiscoveredItem
    url: str = ""
    status_code: int = 200
    content_type: str = ""
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False
    fetched_at: datetime
