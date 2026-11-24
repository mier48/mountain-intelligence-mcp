"""Price lookup used by recommendations. No currency conversion without an explicit rate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from mountain_intelligence.catalog.models import Product

from .models import Offer


@dataclass(frozen=True)
class CurrentPrice:
    amount: Decimal
    currency: str
    retailer: str
    url: str
    observed_at: str
    availability: str
    is_synthetic: bool


def current_offers(product: Product, *, include_synthetic: bool) -> list[Offer]:
    cutoff = timezone.now() - timedelta(days=settings.MI_OFFER_MAX_AGE_DAYS)
    qs = product.offers.filter(observed_at__gte=cutoff).exclude(availability=Offer.Availability.OUT_OF_STOCK)
    if not include_synthetic:
        qs = qs.exclude(data_status="synthetic_fixture")
    return list(qs.select_related("variant").order_by("price")[:10])


def availability_summary(product: Product, *, include_synthetic: bool) -> dict[str, int]:
    """Recent offers per availability state (out-of-stock variants included)."""
    cutoff = timezone.now() - timedelta(days=settings.MI_OFFER_MAX_AGE_DAYS)
    qs = product.offers.filter(observed_at__gte=cutoff)
    if not include_synthetic:
        qs = qs.exclude(data_status="synthetic_fixture")
    summary: dict[str, int] = {}
    for state in qs.values_list("availability", flat=True):
        summary[state] = summary.get(state, 0) + 1
    return summary


def comparable_price_eur(product: Product, *, include_synthetic: bool) -> CurrentPrice | None:
    """Lowest recent EUR offer. Offers in other currencies are ignored (not converted)."""
    for offer in current_offers(product, include_synthetic=include_synthetic):
        if offer.currency == "EUR":
            return CurrentPrice(
                offer.price,
                "EUR",
                offer.retailer,
                offer.url,
                offer.observed_at.isoformat(),
                offer.availability,
                offer.data_status == "synthetic_fixture",
            )
    return None
