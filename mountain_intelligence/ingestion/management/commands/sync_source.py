"""Run (or enqueue) the ingestion of one enabled source."""

from __future__ import annotations

from django.core.management.base import CommandError, CommandParser

from mountain_intelligence.ingestion.models import IngestionRun
from mountain_intelligence.ingestion.pipeline import SourceNotSyncable, run_source_sync
from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.models import Source


class Command(JsonCommand):
    help = "Synchronize a source now (default) or enqueue it on the Celery worker (--async)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("slug")
        parser.add_argument("--actor", default="cli")
        parser.add_argument("--max-items", type=int, default=None, help="Process at most N discovered items")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Process the next N items of the sync cycle (overrides config.batch_size)",
        )
        parser.add_argument(
            "--restart-cycle", action="store_true", help="Discard the batch cursor and start a new sync cycle"
        )
        parser.add_argument("--dry-run", action="store_true", help="Run the pipeline but roll back all writes")
        parser.add_argument("--async", dest="run_async", action="store_true", help="Enqueue on Celery")
        parser.add_argument(
            "--test-disabled",
            action="store_true",
            help="Allow a single run of a disabled source whose access is authorized "
            "(adapter validation before enabling)",
        )

    def handle(self, *args, **options):
        source = Source.objects.filter(slug=options["slug"]).first()
        if source is None:
            raise CommandError(f"unknown source {options['slug']!r}")
        if options["run_async"]:
            if not source.can_sync:
                raise CommandError(f"source {source.slug} is not enabled/authorized")
            from mountain_intelligence.ingestion.tasks import sync_source

            result = sync_source.delay(source.slug, IngestionRun.Trigger.CLI, options["actor"], options["max_items"])
            self.emit(options, {"enqueued": True, "task_id": result.id}, [f"enqueued task {result.id}"])
            return
        try:
            run = run_source_sync(
                source,
                trigger=IngestionRun.Trigger.CLI,
                actor=options["actor"],
                dry_run=options["dry_run"],
                max_items=options["max_items"],
                ignore_enabled=options["test_disabled"],
                batch_size=options["batch_size"],
                restart_cycle=options["restart_cycle"],
            )
        except SourceNotSyncable as exc:
            raise CommandError(str(exc)) from exc
        data = {
            "run_id": str(run.run_id),
            "status": run.status,
            "dry_run": run.dry_run,
            "discovery_complete": run.discovery_complete,
            "catalog_complete": run.catalog_complete,
            "batch": run.batch,
            "metrics": run.metrics,
            "errors": run.errors[:20],
        }
        lines = [
            f"run {run.run_id} [{run.status}]{' (dry-run)' if run.dry_run else ''}",
            f"  metrics: { {k: v for k, v in run.metrics.items() if k != 'warnings'} }",
            f"  discovery_complete={run.discovery_complete} catalog_complete={run.catalog_complete}"
            + (
                f" batch {run.batch.get('already_done', 0) + run.batch.get('this_batch', 0)}/{run.batch.get('total')}"
                if run.batch.get("mode") == "batch"
                else ""
            ),
        ]
        lines += [f"  warning: {w}" for w in run.metrics.get("warnings", [])[:10]]
        lines += [f"  error {e['kind']}: {e['item']} - {e['message']}" for e in run.errors[:20]]
        self.emit(options, data, lines)
        if run.status == IngestionRun.Status.FAILED:
            raise CommandError("ingestion run failed")
