"""Celery tasks: scheduled dispatch of due sources and per-source sync."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from mountain_intelligence.sources.models import Source

from .models import IngestionRun
from .pipeline import SourceNotSyncable, cycle_in_progress, run_source_sync

logger = logging.getLogger(__name__)


@shared_task(
    name="mountain_intelligence.ingestion.tasks.sync_source", ignore_result=True, autoretry_for=(), acks_late=True
)
def sync_source(
    source_slug: str, trigger: str = IngestionRun.Trigger.SCHEDULED, actor: str = "", max_items: int | None = None
) -> str | None:
    source = Source.objects.filter(slug=source_slug).first()
    if source is None:
        logger.warning("sync requested for unknown source", extra={"source": source_slug})
        return None
    try:
        run = run_source_sync(source, trigger=trigger, actor=actor, max_items=max_items)
    except SourceNotSyncable as exc:
        logger.warning("source not syncable", extra={"source": source_slug, "reason": str(exc)})
        return None
    return str(run.run_id)


def due_sources(now=None) -> list[Source]:
    """Sources whose next run is due.

    A source in the middle of a batched sync cycle is due again after
    ``config.batch_interval_minutes`` (default 0: next scheduler tick) until the cycle completes;
    otherwise the source waits ``sync_frequency_hours`` after its last run.
    """
    now = now or timezone.now()
    due: list[Source] = []
    for source in Source.objects.filter(
        enabled=True, access_status__in=Source.ACCESS_ALLOWING_SYNC, sync_frequency_hours__isnull=False
    ):
        last = source.runs.exclude(status=IngestionRun.Status.SKIPPED).order_by("-started_at").first()
        if cycle_in_progress(source):
            wait = timedelta(minutes=int(source.config.get("batch_interval_minutes", 0)))
            if last is not None and last.status == IngestionRun.Status.FAILED:
                wait = max(wait, timedelta(hours=1))  # do not hammer a failing site every tick
        else:
            wait = timedelta(hours=source.sync_frequency_hours or 0)
        if last is None or last.started_at <= now - wait:
            due.append(source)
    return due


@shared_task(name="mountain_intelligence.ingestion.tasks.dispatch_due_syncs", ignore_result=True)
def dispatch_due_syncs() -> list[str]:
    if not settings.INGESTION_SCHEDULE_ENABLED:
        return []
    slugs = [s.slug for s in due_sources()]
    for slug in slugs:
        sync_source.delay(slug, IngestionRun.Trigger.SCHEDULED)
    if slugs:
        logger.info("dispatched scheduled syncs", extra={"sources": slugs})
    return slugs
