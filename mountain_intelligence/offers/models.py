"""Optional commercial offers, kept independent from the technical catalog."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from mountain_intelligence.catalog.models import DataStatus, Product, ProductVariant
from mountain_intelligence.sources.models import Source


class Offer(models.Model):
    class Availability(models.TextChoices):
        IN_STOCK = "in_stock"
        OUT_OF_STOCK = "out_of_stock"
        PREORDER = "preorder"
        UNKNOWN = "unknown"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="offers")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True, related_name="offers")
    source = models.ForeignKey(Source, on_delete=models.SET_NULL, null=True, blank=True, related_name="offers")
    retailer = models.CharField(max_length=120)
    url = models.URLField(max_length=1000, blank=True)
    currency = models.CharField(max_length=3)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    availability = models.CharField(max_length=20, choices=Availability.choices, default=Availability.UNKNOWN)
    observed_at = models.DateTimeField()
    data_status = models.CharField(max_length=30, choices=DataStatus.choices)

    class Meta:
        ordering = ["product_id", "price"]
        constraints = [
            models.CheckConstraint(condition=Q(price__gt=0), name="offer_price_positive"),
            models.CheckConstraint(condition=Q(currency__regex=r"^[A-Z]{3}$"), name="offer_currency_iso"),
            models.UniqueConstraint(
                fields=["product", "variant", "retailer"], nulls_distinct=False, name="unique_offer_per_retailer"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.retailer}: {self.price} {self.currency}"


class PriceObservation(models.Model):
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="observations")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    availability = models.CharField(max_length=20, choices=Offer.Availability.choices)
    observed_at = models.DateTimeField()

    class Meta:
        ordering = ["offer_id", "-observed_at"]
        constraints = [
            models.UniqueConstraint(fields=["offer", "observed_at"], name="unique_offer_observation"),
            models.CheckConstraint(condition=Q(price__gt=0), name="observation_price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.offer_id}@{self.observed_at:%Y-%m-%d}: {self.price} {self.currency}"
