"""Shared helpers for management commands (human-readable or JSON output, exit codes)."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class JsonCommand(BaseCommand):
    """Adds ``--json`` for automation. Errors raise CommandError (exit code 1)."""

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    def emit(self, options: dict[str, Any], data: Any, lines: list[str] | None = None) -> None:
        if options.get("json"):
            self.stdout.write(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        else:
            for line in lines if lines is not None else [json.dumps(data, indent=2, default=str)]:
                self.stdout.write(line)
