"""Celery application (worker + beat share the application image and Django models)."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mountain_intelligence.config.settings")

app = Celery("mountain_intelligence")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["mountain_intelligence.ingestion"])


@app.on_after_finalize.connect
def _setup_periodic_tasks(sender: Celery, **_: object) -> None:
    from django.conf import settings

    # A single dispatcher decides which enabled + authorized sources are due, honouring each
    # source's own frequency. Nothing runs unless INGESTION_SCHEDULE_ENABLED is true.
    sender.add_periodic_task(
        float(settings.INGESTION_SCHEDULER_INTERVAL_SECONDS),
        sender.signature("mountain_intelligence.ingestion.tasks.dispatch_due_syncs"),
        name="dispatch-due-source-syncs",
    )
