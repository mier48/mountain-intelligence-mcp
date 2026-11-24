"""Operator CLI, scheduling, registry validation and logging hygiene."""

from __future__ import annotations

import io
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.utils import timezone

from mountain_intelligence.ingestion import tasks
from mountain_intelligence.ingestion.models import IngestionRun, ReviewItem
from mountain_intelligence.ops.logging import JsonFormatter, redact
from mountain_intelligence.recommendations.models import ActivityProfile
from mountain_intelligence.sources.models import Source

from .conftest import boot, write_products

pytestmark = pytest.mark.django_db


def run(*args, **kwargs) -> str:
    out = io.StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


def test_source_registry_loads_and_is_idempotent():
    first = json.loads(run("import_sources", "--json"))
    second = json.loads(run("import_sources", "--json"))
    total = Source.objects.count()
    assert total == 15
    assert first["sources_created"] == total and second["sources_updated"] == total
    assert first["brands"] == 27
    # only sources whose access allows it are enabled (also enforced by a DB constraint)
    for source in Source.objects.filter(enabled=True):
        assert source.access_status in Source.ACCESS_ALLOWING_SYNC
    petzl = Source.objects.get(slug="petzl-website")
    assert petzl.access_status == "restricted" and not petzl.enabled
    altus = Source.objects.get(slug="altus-website")
    assert altus.adapter == "shopify_product" and altus.categories == [] and altus.unrestricted_categories
    listing = json.loads(run("list_sources", "--json"))
    assert {r["slug"] for r in listing} >= {"petzl-website", "scarpa-website"}


def test_registry_rejects_enabled_unauthorized_source(tmp_path: Path):
    bad = tmp_path / "sources.yaml"
    bad.write_text(
        "version: 1\nsources:\n  - {slug: x-site, name: X, source_type: manufacturer_website, "
        "adapter: jsonld_product, allowed_domains: [x.test], enabled: true}\n"
    )
    with pytest.raises(CommandError, match="cannot be enabled"):
        run("import_sources", str(bad))


def test_enabling_requires_access_and_db_constraint():
    run("import_sources")
    with pytest.raises(CommandError, match="cannot enable"):
        run("set_source_status", "petzl-website", "--enable", "--actor", "alice", "--reason", "try")
    run(
        "set_source_status",
        "petzl-website",
        "--access",
        "authorized",
        "--enable",
        "--actor",
        "alice",
        "--reason",
        "written permission ref 2026-11-24",
    )
    source = Source.objects.get(slug="petzl-website")
    assert source.enabled and "written permission" in source.access_notes
    with pytest.raises(IntegrityError):
        Source.objects.filter(pk=source.pk).update(access_status="restricted")


def test_import_products_dry_run_and_review_cli(structured_source, import_dir):
    structured_source(auto_publish=False)
    path = import_dir / "batch.json"
    write_products(path, [boot(), boot(external_id="b2", name="Broken", specifications={"rigidity": "bendy"})])
    dry = json.loads(
        run("import_products", str(path), "--source", "authorized-import", "--actor", "op", "--dry-run", "--json")
    )
    assert dry["outcomes"]["new"] == 2 and dry["run_id"] is None
    assert not ReviewItem.objects.exists()
    real = json.loads(run("import_products", str(path), "--source", "authorized-import", "--actor", "op", "--json"))
    assert real["outcomes"]["new"] == 2
    pending = json.loads(run("list_review_items", "--json"))
    assert {r["kind"] for r in pending} == {"new_product", "invalid_value"}
    item = next(r for r in pending if r["kind"] == "new_product")
    out = run("review_item", str(item["id"]), "--approve", "--actor", "alice", "--reason", "verified")
    assert "approved" in out
    with pytest.raises(CommandError, match="already approved"):
        run("review_item", str(item["id"]), "--reject", "--actor", "alice", "--reason", "again")
    assert IngestionRun.objects.filter(trigger="import").count() == 1


def test_import_requires_authorized_source(structured_source, import_dir):
    source = structured_source()
    Source.objects.filter(pk=source.pk).update(enabled=False, access_status="unverified")
    write_products(import_dir / "x.json", [boot()])
    with pytest.raises(CommandError, match="authorization"):
        run("import_products", str(import_dir / "x.json"), "--source", source.slug, "--actor", "op")


def test_sync_source_and_runs_listing(structured_source):
    structured_source(products=[boot()])
    out = json.loads(run("sync_source", "authorized-import", "--json"))
    assert out["status"] == "succeeded"
    runs = json.loads(run("list_ingestion_runs", "--json"))
    assert runs[0]["metrics"]["new"] == 1
    with pytest.raises(CommandError, match="not syncable"):
        run("import_sources")
        run("sync_source", "petzl-website")


def test_validate_catalog_and_seed_idempotency(demo_catalog):
    report = json.loads(run("validate_catalog", "--json", "--strict"))
    assert report["errors"] == [] and report["products_checked"] == 34
    again = json.loads(run("seed_demo_data", "--json"))
    assert again["metrics"]["unchanged"] == 34 and again["metrics"]["new"] == 0


def test_profiles_are_versioned(reference_data, tmp_path: Path):
    assert ActivityProfile.objects.filter(is_active=True).count() == 5
    assert set(ActivityProfile.objects.filter(is_active=True).values_list("key", flat=True)) == {
        "winter_mountaineering",
        "summer_alpine_mountaineering",
        "winter_hiking",
        "hiking_trekking",
        "trail_running",
    }
    edited = (
        Path("data/activity_profiles.yaml")
        .read_text()
        .replace("Winter mountaineering\n", "Winter mountaineering EDITED\n", 1)
        .replace("Non-technical to moderate", "Edited description", 1)
    )
    path = tmp_path / "p.yaml"
    path.write_text(edited)
    with pytest.raises(CommandError, match="bump the version"):
        run("load_activity_profiles", str(path))


def test_scheduler_only_dispatches_due_enabled_sources(structured_source, settings, monkeypatch):
    source = structured_source(products=[boot()])
    Source.objects.filter(pk=source.pk).update(sync_frequency_hours=24)
    sent: list[str] = []
    monkeypatch.setattr(tasks.sync_source, "delay", lambda slug, *a: sent.append(slug))
    settings.INGESTION_SCHEDULE_ENABLED = False
    assert tasks.dispatch_due_syncs() == []
    settings.INGESTION_SCHEDULE_ENABLED = True
    assert tasks.dispatch_due_syncs() == ["authorized-import"] and sent == ["authorized-import"]
    IngestionRun.objects.create(source=source, trigger="scheduled", status="succeeded")
    assert tasks.due_sources() == []
    IngestionRun.objects.update(started_at=timezone.now() - timedelta(hours=25))
    assert [s.slug for s in tasks.due_sources()] == ["authorized-import"]


def test_sync_task_runs_pipeline(structured_source):
    structured_source(products=[boot()])
    assert tasks.sync_source("authorized-import")
    assert tasks.sync_source("unknown-source") is None


def test_logs_redact_secrets():
    assert "abc123" not in redact("Authorization: Bearer abc123")
    assert "s3cret" not in redact("postgres://user:s3cret@db:5432/x")
    assert "hunter2" not in redact("password=hunter2 token: zzz")
    record = logging.makeLogRecord({"msg": "token=%s", "args": ("leak",), "levelno": 20, "levelname": "INFO"})
    assert "leak" not in JsonFormatter().format(record)


def test_registry_documentation_fields_and_validation(tmp_path: Path):
    from mountain_intelligence.sources.registry import load_registry

    registry = load_registry(Path("data/sources.yaml"))
    assert registry.candidate_sources and all(
        c.status != "to_evaluate" or c.notes or c.route for c in registry.candidate_sources
    )
    assert all(b.verified_at for b in registry.brands if b.domain_verified)
    run("import_sources")
    petzl = Source.objects.get(slug="petzl-website")
    assert "References: https://www.petzl.com/INT/en/legal" in petzl.access_notes
    bad = tmp_path / "dupe.yaml"
    bad.write_text("version: 1\nbrands:\n  - {name: X}\n  - {name: x}\n")
    with pytest.raises(CommandError, match="duplicate brand"):
        run("import_sources", str(bad), "--check")
    unverified = tmp_path / "verified.yaml"
    unverified.write_text("version: 1\nbrands:\n  - {name: X, domain_verified: true}\n")
    with pytest.raises(CommandError, match="verified domain"):
        run("import_sources", str(unverified), "--check")


def test_rule_parameters_are_validated(tmp_path: Path):
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        "version: 1\nrules:\n  - key: bad-rule\n    version: 1\n    relation: boot_crampon\n"
        "    source_category: mountaineering_boot\n    target_category: crampon\n"
        "    evaluator: binding_type_match\n    description: x\n"
        "    parameters: {rating_matrix: {B9: [C1]}}\n"
    )
    with pytest.raises(CommandError, match="rating_matrix"):
        run("load_compatibility_rules", str(bad))


def test_upgrading_rules_retires_previous_version(reference_data):
    from mountain_intelligence.recommendations.models import CompatibilityRule

    statuses = dict(CompatibilityRule.objects.values_list("version", "status"))
    assert statuses == {1: "retired", 2: "published"}
