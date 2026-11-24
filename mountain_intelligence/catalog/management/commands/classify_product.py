"""Assign a technical category to a product by hand (progressive enrichment of the catalog).

Typical use: an `uncategorized` product from a full-catalog source turns out to be, say, a backpack.
The product keeps its id, variants, offers, sources and original attributes; from now on the strict
schema of the new category applies and the product becomes eligible for technical features once it
has specification data. Existing specifications must validate against the new schema.
"""

from __future__ import annotations

from django.core.management.base import CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from mountain_intelligence.catalog.models import Category, Product
from mountain_intelligence.catalog.normalization import normalize_specifications
from mountain_intelligence.catalog.services import refresh_derived_and_search
from mountain_intelligence.catalog.taxonomy import CATEGORIES
from mountain_intelligence.ops.cli import JsonCommand


class Command(JsonCommand):
    help = "Move a product to another category (e.g. from `uncategorized` to a technical category)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("product_id")
        parser.add_argument("category", choices=sorted(CATEGORIES))
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        product = Product.objects.select_for_update().filter(public_id=options["product_id"]).first()
        if product is None:
            raise CommandError(f"unknown product {options['product_id']!r}")
        if not options["actor"].strip() or not options["reason"].strip():
            raise CommandError("actor and reason are mandatory")
        definition = CATEGORIES[options["category"]]
        previous = product.category.slug
        if previous == definition.slug:
            raise CommandError(f"{product.public_id} is already {definition.slug}")
        norm = normalize_specifications(definition, product.specifications or {})
        if norm.issues:
            details = "; ".join(f"{i.field}: {i.code}" for i in norm.issues[:10])
            raise CommandError(f"current specifications do not validate against {definition.slug}: {details}")
        clash = (
            Product.objects.filter(
                brand_id=product.brand_id,
                category__slug=definition.slug,
                name=product.name,
                generation=product.generation,
            )
            .exclude(pk=product.pk)
            .first()
        )
        if clash is not None:
            raise CommandError(f"{clash.public_id} already exists in {definition.slug} with the same name/generation")
        product.category = Category.objects.get(slug=definition.slug)
        product.category_basis = Product.CategoryBasis.REVIEW
        product.specifications = norm.specifications
        product.spec_schema_version = definition.schema_version
        product.save()
        refresh_derived_and_search(product)
        from mountain_intelligence.ingestion.models import ReviewItem

        ReviewItem.objects.create(  # audit trail of the decision (who, when, why)
            kind=ReviewItem.Kind.RECLASSIFICATION,
            status=ReviewItem.Status.APPROVED,
            product=product,
            field="category",
            current_value=previous,
            proposed_value=definition.slug,
            reason="Manual classification",
            decided_at=timezone.now(),
            decided_by=options["actor"].strip()[:120],
            decision_reason=options["reason"].strip(),
        )
        data = {
            "product_id": product.public_id,
            "from": previous,
            "to": definition.slug,
            "actor": options["actor"].strip()[:120],
            "reason": options["reason"].strip(),
        }
        self.emit(options, data, [f"{product.public_id}: {previous} -> {definition.slug} ({data['actor']})"])
