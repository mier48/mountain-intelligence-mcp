"""Load versioned activity profiles from YAML (idempotent; edits require a version bump)."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from pydantic import ValidationError

from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.recommendations.profiles import load_profiles


class Command(JsonCommand):
    help = "Validate and load activity profiles."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("path", nargs="?", type=Path, default=None)

    def handle(self, *args, **options):
        path = options["path"] or settings.DATA_DIR / "activity_profiles.yaml"
        try:
            stats = load_profiles(path)
        except (OSError, ValidationError, ValueError) as exc:
            raise CommandError(f"cannot load profiles from {path}: {exc}") from exc
        self.emit(options, stats, [f"activity profiles: {stats}"])
