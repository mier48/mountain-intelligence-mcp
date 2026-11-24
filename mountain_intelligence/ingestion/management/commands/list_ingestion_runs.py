"""List recent ingestion runs with metrics and errors."""

from __future__ import annotations

from django.core.management.base import CommandParser

from mountain_intelligence.ingestion.models import IngestionRun
from mountain_intelligence.ops.cli import JsonCommand


class Command(JsonCommand):
    help = "Show recent ingestion runs (optionally for one source)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--source")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--errors", action="store_true", help="Include error details")

    def handle(self, *args, **options):
        qs = IngestionRun.objects.select_related("source")
        if options["source"]:
            qs = qs.filter(source__slug=options["source"])
        rows = [
            {
                "run_id": str(r.run_id),
                "source": r.source.slug,
                "trigger": r.trigger,
                "status": r.status,
                "dry_run": r.dry_run,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "metrics": {k: v for k, v in r.metrics.items() if k != "warnings"},
                **({"errors": r.errors} if options["errors"] else {"error_count": len(r.errors)}),
            }
            for r in qs[: max(1, min(options["limit"], 500))]
        ]
        lines = [f"{r['started_at'][:19]} {r['source']:28} {r['status']:10} {r['metrics']}" for r in rows]
        if options["errors"]:
            for r in rows:
                lines += [f"    {r['run_id']} {e['kind']}: {e['item']} {e['message']}" for e in r["errors"][:20]]
        self.emit(options, rows, lines or ["no runs"])
