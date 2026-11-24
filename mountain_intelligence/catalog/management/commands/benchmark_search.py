"""Measure representative query latency on a reproducible synthetic dataset (rolled back afterwards)."""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Callable
from typing import Any

from django.core.management.base import CommandParser
from django.db import transaction

from mountain_intelligence.catalog.models import Brand, Category, DataStatus, Product
from mountain_intelligence.catalog.queries import SpecFilter, compare_products, product_detail, search_products
from mountain_intelligence.catalog.services import build_search_document
from mountain_intelligence.catalog.taxonomy import get_category
from mountain_intelligence.ops.cli import JsonCommand


class _Rollback(Exception):
    pass


def _boot_specs(rng: random.Random) -> dict[str, Any]:
    return {
        "weight_g": rng.randint(550, 1100),
        "weight_basis": rng.choice(["per_boot", "per_pair", None]),
        "rigidity": rng.choice(["flexible", "semi_rigid", "rigid"]),
        "crampon_attachment_types": rng.choice(
            [["strap_on"], ["strap_on", "semi_automatic"], ["strap_on", "semi_automatic", "automatic"], None]
        ),
        "waterproof": rng.choice([True, False, None]),
        "heel_welt": rng.choice([True, False, None]),
    }


def _crampon_specs(rng: random.Random) -> dict[str, Any]:
    return {
        "binding_types": rng.choice([["strap_on"], ["semi_automatic"], ["automatic"]]),
        "points": rng.choice([10, 12, 14]),
        "material": rng.choice(["steel", "aluminium"]),
        "weight_g": rng.randint(500, 1200),
        "weight_basis": "per_pair",
        "anti_balling_plates": rng.random() > 0.3,
    }


class Command(JsonCommand):
    help = "Benchmark search/detail/compare on N generated synthetic products inside a rolled-back transaction."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--products", type=int, default=5000)
        parser.add_argument("--repeat", type=int, default=30)
        parser.add_argument("--seed", type=int, default=42)

    def handle(self, *args, **options):
        rng = random.Random(options["seed"])  # noqa: S311 - reproducible synthetic data, not security
        results: dict[str, Any] = {}
        try:
            with transaction.atomic():
                self._populate(rng, options["products"])
                results = self._measure(options["repeat"], options["products"])
                raise _Rollback
        except _Rollback:
            pass
        lines = [f"dataset: {options['products']} synthetic products (rolled back); repeat={options['repeat']}"]
        lines += [
            f"  {name:42} p50={r['p50_ms']:7.2f} ms  p95={r['p95_ms']:7.2f} ms  max={r['max_ms']:7.2f} ms"
            for name, r in results.items()
        ]
        self.emit(options, results, lines)

    def _populate(self, rng: random.Random, n: int) -> None:
        brands = [
            Brand.objects.create(name=f"Bench Brand {i}", slug=f"bench-brand-{i}", is_synthetic=True) for i in range(25)
        ]
        cats = {c.slug: c for c in Category.objects.filter(slug__in=["mountaineering_boot", "crampon"])}
        words = ["alpine", "summit", "ridge", "glacier", "north", "face", "couloir", "peak", "storm", "ice"]
        batch = []
        for i in range(n):
            slug = "mountaineering_boot" if i % 2 else "crampon"
            specs = _boot_specs(rng) if slug == "mountaineering_boot" else _crampon_specs(rng)
            specs = {k: v for k, v in specs.items() if v is not None}
            product = Product(
                public_id=f"bench-{i}",
                brand=rng.choice(brands),
                category=cats[slug],
                name=f"{rng.choice(words).title()} {rng.choice(words).title()} {i}",
                specifications=specs,
                derived_specifications=get_category(slug).compute_derived(specs),
                publication_status=Product.PublicationStatus.PUBLISHED,
                data_status=DataStatus.SYNTHETIC_FIXTURE,
            )
            batch.append(product)
        for product in batch:
            product.search_document = build_search_document(product)
        Product.objects.bulk_create(batch, batch_size=1000)

    def _measure(self, repeat: int, n: int) -> dict[str, Any]:
        cases: dict[str, Callable[[], Any]] = {
            "text: 'botas alpinismo glacier'": lambda: search_products(
                query="botas alpinismo glacier", include_synthetic=True
            ),
            "text no match -> relaxed + trigram": lambda: search_products(
                query="glaciar norte", include_synthetic=True
            ),
            "filters: boots weight+types": lambda: search_products(
                category="mountaineering_boot",
                include_synthetic=True,
                filters=[
                    SpecFilter(field="weight_per_boot_g", op="lte", value=800),
                    SpecFilter(field="crampon_attachment_types", op="contains", value=["semi_automatic"]),
                ],
            ),
            "text+filters: crampons steel": lambda: search_products(
                query="ice",
                category="crampon",
                include_synthetic=True,
                filters=[SpecFilter(field="material", op="eq", value="steel")],
            ),
            "identifier lookup": lambda: search_products(query=f"bench-{n // 2}", include_synthetic=True),
            "get_product detail": lambda: product_detail(f"bench-{n // 3}", include_synthetic=True),
            "compare 3 products": lambda: compare_products(
                [f"bench-{1}", f"bench-{3}", f"bench-{5}"], include_synthetic=True
            ),
        }
        out = {}
        for name, fn in cases.items():
            fn()  # warm-up
            timings = []
            for _ in range(repeat):
                start = time.perf_counter()
                fn()
                timings.append((time.perf_counter() - start) * 1000)
            timings.sort()
            out[name] = {
                "p50_ms": round(statistics.median(timings), 2),
                "p95_ms": round(timings[max(0, int(len(timings) * 0.95) - 1)], 2),
                "max_ms": round(timings[-1], 2),
            }
        return out
