"""Enable/disable a source or record its access review (audited in access_notes)."""

from __future__ import annotations

from django.core.management.base import CommandError, CommandParser
from django.utils import timezone

from mountain_intelligence.ops.cli import JsonCommand
from mountain_intelligence.sources.models import Source


class Command(JsonCommand):
    help = "Change a source's access status and/or enablement. Enabling requires authorized/terms_permit access."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("slug")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--enable", action="store_true")
        group.add_argument("--disable", action="store_true")
        parser.add_argument("--access", choices=Source.AccessStatus.values)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        source = Source.objects.filter(slug=options["slug"]).first()
        if source is None:
            raise CommandError(f"unknown source {options['slug']!r}")
        if options["access"]:
            source.access_status = options["access"]
            source.terms_reviewed_at = timezone.now().date()
            if source.access_status not in Source.ACCESS_ALLOWING_SYNC:
                source.enabled = False
        if options["enable"]:
            if source.access_status not in Source.ACCESS_ALLOWING_SYNC:
                raise CommandError(f"cannot enable {source.slug}: access_status is {source.access_status!r}")
            source.enabled = True
        if options["disable"]:
            source.enabled = False
        stamp = (
            f"[{timezone.now():%Y-%m-%d %H:%M} {options['actor']}] access={source.access_status} "
            f"enabled={source.enabled}: {options['reason']}"
        )
        source.access_notes = (source.access_notes + "\n" + stamp).strip()
        source.save()
        self.emit(
            options, {"slug": source.slug, "access_status": source.access_status, "enabled": source.enabled}, [stamp]
        )
