"""Load the SYNTHETIC demo catalog through the regular ingestion pipeline (idempotent)."""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from mountain_intelligence.catalog.models import DataStatus, Product
from mountain_intelligence.ingestion.models import IngestionRun
from mountain_intelligence.ingestion.pipeline import run_source_sync
from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.recommendations.models import CompatibilityAssertion
from mountain_intelligence.sources.models import Source

DEMO_SOURCE = "demo-synthetic-catalog"


class Command(JsonCommand):
    help = "Load synthetic demo fixtures (excluded from MCP answers unless MI_INCLUDE_SYNTHETIC_DATA=true)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--catalog", default="demo/catalog.json", help="Path relative to the fixtures dir")

    def handle(self, *args, **options):
        with transaction.atomic():
            source, _ = Source.objects.update_or_create(
                slug=DEMO_SOURCE,
                defaults={
                    "name": "Synthetic demo catalog (not real products)",
                    "source_type": Source.SourceType.SYNTHETIC,
                    "adapter": "structured_file",
                    "access_status": Source.AccessStatus.AUTHORIZED,
                    "access_notes": "Synthetic fixtures generated for demos and tests.",
                    "enabled": True,
                    "auto_publish": True,
                    "is_synthetic": True,
                    "sync_frequency_hours": None,
                    "config": {"path": options["catalog"]},
                },
            )
        run = run_source_sync(source, trigger=IngestionRun.Trigger.SEED, actor="seed_demo_data")
        if run.status == IngestionRun.Status.FAILED:
            raise CommandError(f"demo ingestion failed: {run.errors}")
        created = self._load_assertions()
        data = {
            "run_id": str(run.run_id),
            "status": run.status,
            "metrics": run.metrics,
            "assertions_created": created,
            "synthetic_products": Product.objects.filter(data_status=DataStatus.SYNTHETIC_FIXTURE).count(),
            "mcp_includes_synthetic_data": settings.MI_INCLUDE_SYNTHETIC_DATA,
        }
        lines = [
            f"Demo ingestion run {run.run_id}: {run.status}",
            f"  metrics: {json.dumps({k: v for k, v in run.metrics.items() if k != 'warnings'})}",
            f"  pair-level assertions created: {created}",
            f"  synthetic products in catalog: {data['synthetic_products']}",
        ]
        if not settings.MI_INCLUDE_SYNTHETIC_DATA:
            lines.append("  NOTE: MI_INCLUDE_SYNTHETIC_DATA is false, so MCP tools will NOT show this demo data.")
        self.emit(options, data, lines)

    def _load_assertions(self) -> int:
        path = settings.FIXTURES_DIR / "demo" / "compatibility_assertions.json"
        items = json.loads(path.read_text(encoding="utf-8"))["assertions"]
        created = 0
        for item in items:
            a = Product.objects.filter(public_id=item["product_a"]).first()
            b = Product.objects.filter(public_id=item["product_b"]).first()
            if a is None or b is None:
                raise CommandError(f"assertion references unknown products: {item['product_a']}, {item['product_b']}")
            _, was_created = CompatibilityAssertion.objects.update_or_create(
                relation=item["relation"],
                product_a=a,
                product_b=b,
                declared_by=item["declared_by"],
                defaults={
                    "verdict": item["verdict"],
                    "conditions": item.get("conditions", ""),
                    "source_url": item.get("source_url", ""),
                    "observed_at": timezone.now(),
                    "data_status": DataStatus.SYNTHETIC_FIXTURE,
                },
            )
            created += int(was_created)
        return created
