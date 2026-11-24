"""Show sources with access status, enablement, last run and errors."""

from __future__ import annotations

from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.models import Source


class Command(JsonCommand):
    help = "List registered sources and their last ingestion run."

    def handle(self, *args, **options):
        rows = []
        for s in Source.objects.select_related("brand").order_by("slug"):
            last = s.runs.order_by("-started_at").first()
            rows.append(
                {
                    "slug": s.slug,
                    "name": s.name,
                    "brand": s.brand.name if s.brand else None,
                    "type": s.source_type,
                    "adapter": s.adapter,
                    "access_status": s.access_status,
                    "enabled": s.enabled,
                    "synthetic": s.is_synthetic,
                    "frequency_h": s.sync_frequency_hours,
                    "last_run": None
                    if last is None
                    else {
                        "run_id": str(last.run_id),
                        "status": last.status,
                        "started_at": last.started_at.isoformat(),
                        "metrics": {k: v for k, v in last.metrics.items() if k != "warnings"},
                        "errors": len(last.errors),
                    },
                }
            )
        lines = [f"{'SLUG':32} {'ACCESS':12} {'ENABLED':8} {'LAST RUN':28}"]
        for r in rows:
            last = r["last_run"]
            last_txt = f"{last['status']} {last['started_at'][:19]}" if last else "never"
            lines.append(f"{r['slug']:32} {r['access_status']:12} {r['enabled']!s:8} {last_txt:28}")
        self.emit(options, rows, lines)
