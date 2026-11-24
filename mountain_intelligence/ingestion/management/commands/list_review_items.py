"""List review queue items (pending by default)."""

from __future__ import annotations

from django.core.management.base import CommandParser

from mountain_intelligence.ingestion.models import ReviewItem
from mountain_intelligence.ops.cli import JsonCommand


class Command(JsonCommand):
    help = "Show review items: new products, critical changes, conflicts, invalid values, missing at source."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--status", default="pending", choices=[*ReviewItem.Status.values, "all"])
        parser.add_argument("--kind", choices=ReviewItem.Kind.values)
        parser.add_argument("--product")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        qs = ReviewItem.objects.select_related("product", "source_record__source")
        if options["status"] != "all":
            qs = qs.filter(status=options["status"])
        if options["kind"]:
            qs = qs.filter(kind=options["kind"])
        if options["product"]:
            qs = qs.filter(product__public_id=options["product"])
        rows = [
            {
                "id": i.pk,
                "kind": i.kind,
                "status": i.status,
                "product": i.product.public_id if i.product else None,
                "source": i.source_record.source.slug if i.source_record else None,
                "field": i.field or None,
                "current": i.current_value,
                "proposed": i.proposed_value,
                "reason": i.reason,
                "created_at": i.created_at.isoformat(),
                "decided_by": i.decided_by or None,
                "decision_reason": i.decision_reason or None,
            }
            for i in qs[: max(1, min(options["limit"], 1000))]
        ]
        lines = [
            f"#{r['id']:<5} {r['kind']:18} {r['status']:9} {r['product'] or '-':45} "
            f"{(r['field'] or ''):24} {r['current']!r} -> {r['proposed']!r}  | {r['reason'][:80]}"
            for r in rows
        ]
        self.emit(options, rows, lines or ["no review items"])
