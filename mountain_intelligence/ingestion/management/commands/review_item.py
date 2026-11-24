"""Approve or reject a review item with mandatory actor and justification."""

from __future__ import annotations

import json

from django.core.management.base import CommandError, CommandParser
from pydantic import ValidationError

from mountain_intelligence.catalog.units import UnitError
from mountain_intelligence.ingestion.models import ReviewItem
from mountain_intelligence.ingestion.review import ReviewError, decide
from mountain_intelligence.ops.cli import JsonCommand


class Command(JsonCommand):
    help = "Decide a review item. Every decision records actor, reason and timestamp."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("item_id", type=int)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--approve", action="store_true")
        group.add_argument("--reject", action="store_true")
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--value", help="Corrected value (JSON or plain text) for invalid values/conflicts")
        parser.add_argument("--merge-into", help="Ambiguous match: existing product id to update")
        parser.add_argument("--create-new", action="store_true", help="Ambiguous match: create a separate product")

    def handle(self, *args, **options):
        value = None
        if options["value"] is not None:
            try:
                value = json.loads(options["value"])
            except json.JSONDecodeError:
                value = options["value"]
        try:
            item = decide(
                options["item_id"],
                approve=options["approve"],
                actor=options["actor"],
                reason=options["reason"],
                value=value,
                merge_into=options["merge_into"],
                create_new=options["create_new"],
            )
        except ReviewItem.DoesNotExist as exc:
            raise CommandError(f"review item {options['item_id']} not found") from exc
        except (ReviewError, UnitError, ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        data = {
            "id": item.pk,
            "kind": item.kind,
            "status": item.status,
            "decided_by": item.decided_by,
            "product": item.product.public_id if item.product else None,
        }
        self.emit(options, data, [f"review item #{item.pk} ({item.kind}) -> {item.status} by {item.decided_by}"])
