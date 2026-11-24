"""Detect inconsistencies, missing evidence and stale data in the catalog."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.utils import timezone

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.catalog.normalization import normalize_specifications
from mountain_intelligence.catalog.taxonomy import get_category
from mountain_intelligence.ingestion.models import ReviewItem
from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.models import SpecificationEvidence


class Command(JsonCommand):
    help = "Validate stored products against current schemas and evidence rules (exit 1 with --strict on errors)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--strict", action="store_true", help="Exit with code 1 when errors are found")

    def handle(self, *args, **options):
        errors: list[dict] = []
        warnings: list[dict] = []
        stale_cutoff = timezone.now() - timedelta(days=settings.MI_STALE_AFTER_DAYS)
        published = Product.objects.filter(publication_status=Product.PublicationStatus.PUBLISHED)
        for product in Product.objects.select_related("category").iterator():
            definition = get_category(product.category.slug)
            result = normalize_specifications(definition, product.specifications or {}, units_from_field_names=True)
            for issue in result.issues:
                errors.append({"product": product.public_id, "problem": f"{issue.field}: {issue.code} {issue.message}"})
            if product.spec_schema_version != definition.schema_version:
                warnings.append(
                    {
                        "product": product.public_id,
                        "problem": f"schema v{product.spec_schema_version} != current v{definition.schema_version}",
                    }
                )
            current_fields = set(
                SpecificationEvidence.objects.filter(product=product, is_current=True).values_list("field", flat=True)
            )
            for field, value in (product.specifications or {}).items():
                if value is not None and field not in current_fields:
                    errors.append({"product": product.public_id, "problem": f"{field}: value without current evidence"})
        for product in published.filter(last_verified_at__lt=stale_cutoff):
            warnings.append({"product": product.public_id, "problem": "stale (not re-verified recently)"})
        for product in published.exclude(source_status=Product.SourceStatus.OK):
            warnings.append({"product": product.public_id, "problem": f"source status {product.source_status}"})
        pending = ReviewItem.objects.filter(status=ReviewItem.Status.PENDING).count()
        data = {
            "errors": errors,
            "warnings": warnings,
            "pending_reviews": pending,
            "products_checked": Product.objects.count(),
        }
        lines = [
            f"checked {data['products_checked']} products: {len(errors)} errors, {len(warnings)} warnings, "
            f"{pending} pending reviews"
        ]
        lines += [f"  ERROR {e['product']}: {e['problem']}" for e in errors[:100]]
        lines += [f"  WARN  {w['product']}: {w['problem']}" for w in warnings[:100]]
        self.emit(options, data, lines)
        if options["strict"] and errors:
            raise CommandError(f"{len(errors)} catalog errors")
