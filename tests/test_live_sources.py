"""Opt-in integration against a real, authorized source. Never runs by default.

    MI_RUN_LIVE_SOURCE_TESTS=1 MI_LIVE_SOURCE=<slug> uv run pytest -m live_source

The source must exist (import_sources) with access_status authorized/terms_permit. The run is a
dry-run limited to a few items: nothing is written to the catalog.
"""

from __future__ import annotations

import os

import pytest
from django.core.management import call_command

from mountain_intelligence.ingestion.pipeline import run_source_sync
from mountain_intelligence.sources.models import Source

pytestmark = [
    pytest.mark.live_source,
    pytest.mark.skipif(os.environ.get("MI_RUN_LIVE_SOURCE_TESTS") != "1", reason="live source tests are opt-in"),
]


@pytest.mark.django_db
def test_live_source_dry_run():
    slug = os.environ.get("MI_LIVE_SOURCE")
    if not slug:
        pytest.skip("set MI_LIVE_SOURCE")
    call_command("import_sources")
    source = Source.objects.get(slug=slug)
    if source.access_status not in Source.ACCESS_ALLOWING_SYNC:
        pytest.skip(f"{slug} is not authorized ({source.access_status})")
    run = run_source_sync(source, dry_run=True, max_items=3, ignore_enabled=True)
    assert run.metrics["discovered"] > 0
    assert run.metrics["new"] + run.metrics["unchanged"] + run.metrics["updated"] > 0, run.errors
