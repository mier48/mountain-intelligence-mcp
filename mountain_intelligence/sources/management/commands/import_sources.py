"""Register or update brands and sources from the version-controlled YAML registry."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from pydantic import ValidationError

from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.registry import apply_registry, load_registry


class Command(JsonCommand):
    help = "Validate and apply data/sources.yaml (or another registry file). Idempotent."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("path", nargs="?", type=Path, default=None)
        parser.add_argument("--check", action="store_true", help="Validate only, do not write")

    def handle(self, *args, **options):
        path = options["path"] or settings.DATA_DIR / "sources.yaml"
        try:
            registry = load_registry(path)
        except (OSError, ValidationError) as exc:
            raise CommandError(f"invalid registry {path}: {exc}") from exc
        if options["check"]:
            self.emit(
                options,
                {"valid": True, "brands": len(registry.brands), "sources": len(registry.sources)},
                [f"{path}: valid ({len(registry.brands)} brands, {len(registry.sources)} sources)"],
            )
            return
        try:
            stats = apply_registry(registry)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.emit(options, stats, [f"registry applied: {stats}"])
