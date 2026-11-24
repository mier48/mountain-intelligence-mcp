"""Import an authorized JSON/CSV product file through the validation + review pipeline."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from mountain_intelligence.ingestion.adapters.base import AdapterError
from mountain_intelligence.ingestion.adapters.structured import load_records, parse_record
from mountain_intelligence.ingestion.models import IngestionRun
from mountain_intelligence.ingestion.pipeline import ingest_product
from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.models import Source


class _DryRun(Exception):
    pass


class Command(JsonCommand):
    help = "Import products from an authorized structured file (JSON format v1 or CSV) into a registered source."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("file", type=Path)
        parser.add_argument("--source", required=True, help="Slug of a structured_file source")
        parser.add_argument("--actor", required=True, help="Operator responsible for the import")
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing")

    def handle(self, *args, **options):
        source = Source.objects.filter(slug=options["source"]).first()
        if source is None:
            raise CommandError(f"unknown source {options['source']!r} (register it with import_sources)")
        if source.source_type not in (Source.SourceType.STRUCTURED_FILE, Source.SourceType.SYNTHETIC):
            raise CommandError("import_products only accepts structured_file sources")
        if source.access_status not in Source.ACCESS_ALLOWING_SYNC:
            raise CommandError(
                f"source access_status is {source.access_status!r}; document the authorization "
                "(authorized/terms_permit) before importing"
            )
        path: Path = options["file"]
        if not path.is_file():
            raise CommandError(f"file not found: {path}")
        try:
            records = load_records(path)
        except (AdapterError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        counts: dict[str, int] = {}
        problems: list[str] = []
        run = None
        try:
            with transaction.atomic():
                run = IngestionRun.objects.create(
                    source=source,
                    trigger=IngestionRun.Trigger.IMPORT,
                    dry_run=options["dry_run"],
                    actor=options["actor"],
                )
                for index, record in enumerate(records, start=1):
                    try:
                        extracted = parse_record(record)
                    except AdapterError as exc:
                        counts["invalid"] = counts.get("invalid", 0) + 1
                        problems.append(f"record {index}: {exc}")
                        continue
                    with transaction.atomic():
                        outcome = ingest_product(source, extracted, run=run)
                    counts[outcome.status] = counts.get(outcome.status, 0) + 1
                    counts["reviews_opened"] = counts.get("reviews_opened", 0) + outcome.reviews
                    problems.extend(f"{extracted.external_id}: {m}" for m in outcome.messages)
                run.status = IngestionRun.Status.PARTIAL if problems else IngestionRun.Status.SUCCEEDED
                run.metrics = counts
                run.errors = [{"item": "*", "kind": "invalid", "message": p[:300]} for p in problems[:200]]
                run.save()
                if options["dry_run"]:
                    raise _DryRun
        except _DryRun:
            pass
        data = {
            "dry_run": options["dry_run"],
            "records": len(records),
            "outcomes": counts,
            "problems": problems,
            "run_id": None if options["dry_run"] or run is None else str(run.run_id),
        }
        lines = [f"{'DRY-RUN ' if options['dry_run'] else ''}import of {len(records)} records: {counts}"]
        lines += [f"  - {p}" for p in problems[:50]]
        self.emit(options, data, lines)
