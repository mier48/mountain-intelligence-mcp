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


class ExtractedVariant(StrictModel):
    sku: str = Field(default="", max_length=80)
    mpn: str = Field(default="", max_length=80)
    gtin: str = Field(default="", max_length=14)
    size_label: str = Field(default="", max_length=40)
    size_eu: Decimal | None = Field(default=None, ge=15, le=55)
    color: str = Field(default="", max_length=80)
    spec_overrides: dict[str, Any] = Field(default_factory=dict)

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
    mpn: str = Field(default="", max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=500)
    specifications: dict[str, Any] = Field(default_factory=dict)
    # True when keys carry their unit (weight_g, capacity_l) under an explicit contract.
    units_from_field_names: bool = False
    locators: dict[str, FieldLocator] = Field(default_factory=dict)
    variants: list[ExtractedVariant] = Field(default_factory=list, max_length=500)
    offers: list[ExtractedOffer] = Field(default_factory=list, max_length=50)

    @field_validator("specifications")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 80:
            raise ValueError("too many specification fields")
        return value


class DiscoveredItem(StrictModel):
    external_id: str
    url: str = ""
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
