from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from mountain_intelligence.sources.models import Source


@pytest.fixture(autouse=True, scope="session")
def _no_persistent_connections() -> None:
    """MCP tools run in worker threads; close their connections after each call during tests."""
    from django.db import connections

    connections.settings["default"]["CONN_MAX_AGE"] = 0


@pytest.fixture
def reference_data(db) -> None:
    """Activity profiles + compatibility rules (real, versioned configuration)."""
    call_command("load_activity_profiles", verbosity=0, stdout=_Null())
    call_command("load_compatibility_rules", verbosity=0, stdout=_Null())


@pytest.fixture
def demo_catalog(reference_data, settings) -> None:
    settings.MI_INCLUDE_SYNTHETIC_DATA = True
    call_command("seed_demo_data", stdout=_Null())


@pytest.fixture
def import_dir(tmp_path: Path, settings) -> Path:
    settings.MI_IMPORT_DIR = str(tmp_path)
    return tmp_path


@pytest.fixture
def structured_source(db, import_dir: Path) -> Callable[..., Source]:
    """A real (non-synthetic) authorized structured-file source writing to a temp import dir."""

    def make(
        slug: str = "authorized-import",
        *,
        auto_publish: bool = True,
        products: list[dict] | None = None,
        source_type: str = Source.SourceType.STRUCTURED_FILE,
    ) -> Source:
        source, _ = Source.objects.update_or_create(
            slug=slug,
            defaults={
                "name": slug,
                "source_type": source_type,
                "adapter": "structured_file",
                "access_status": Source.AccessStatus.AUTHORIZED,
                "enabled": True,
                "auto_publish": auto_publish,
                "config": {"path": f"{slug}.json"},
            },
        )
        if products is not None:
            write_products(import_dir / f"{slug}.json", products)
        return source

    return make


def write_products(path: Path, products: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"format": "mountain-intelligence/products", "version": 1, "products": products}))


def boot(**overrides: Any) -> dict[str, Any]:
    base = {
        "external_id": "boot-1",
        "canonical_url": "https://maker.test/boot-1",
        "brand": "Test Maker",
        "name": "Nordwand Test",
        "category": "mountaineering_boot",
        "specifications": {
            "weight_g": 800,
            "weight_basis": "per_boot",
            "rigidity": "semi_rigid",
            "crampon_attachment_types": ["strap_on", "semi_automatic"],
            "heel_welt": True,
            "toe_welt": False,
            "waterproof": True,
        },
    }
    specs = overrides.pop("specifications", None)
    base.update(overrides)
    if specs is not None:
        base["specifications"] = specs
    return base


class _Null:
    def write(self, *_: Any) -> None:
        pass

    def flush(self) -> None:
        pass
