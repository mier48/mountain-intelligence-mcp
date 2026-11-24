"""Load versioned compatibility rules from YAML (idempotent; logic changes require a new version)."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from pydantic import ValidationError

from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.recommendations.compatibility import load_rules


class Command(JsonCommand):
    help = "Validate and load compatibility rules."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("path", nargs="?", type=Path, default=None)

    def handle(self, *args, **options):
        path = options["path"] or settings.DATA_DIR / "compatibility_rules.yaml"
        try:
            stats = load_rules(path)
        except (OSError, ValidationError, ValueError) as exc:
            raise CommandError(f"cannot load rules from {path}: {exc}") from exc
        self.emit(options, stats, [f"compatibility rules: {stats}"])
