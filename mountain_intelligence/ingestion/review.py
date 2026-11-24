"""Applying human review decisions (audited: actor, reason, timestamp)."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from mountain_intelligence.catalog.models import Product
from mountain_intelligence.catalog.normalization import FieldValue, normalize_value
from mountain_intelligence.catalog.services import refresh_derived_and_search
from mountain_intelligence.catalog.taxonomy import get_category
from mountain_intelligence.sources.models import Source, SpecificationEvidence

from .contracts import ExtractedProduct
from .models import ReviewItem
from .pipeline import ingest_product


class ReviewError(Exception):
    pass


def _set_field(item: ReviewItem, value: Any, actor: str, reason: str) -> None:
    product = item.product
    assert product is not None
    definition = get_category(product.category.slug)
    spec = definition.field_map.get(item.field)
    if spec is None or spec.derived:
        raise ReviewError(f"field {item.field!r} cannot be set on {definition.slug}")
    unit = (item.payload or {}).get("unit")
    fv: FieldValue = normalize_value(spec, value, assumed_unit=unit or spec.unit)
    candidate = {**product.specifications, item.field: fv.normalized}
    definition.model.model_validate(candidate)
    SpecificationEvidence.objects.filter(product=product, field=item.field, is_current=True, variant=None).update(
        is_current=False, status=SpecificationEvidence.Status.SUPERSEDED
    )
    pending = (
        SpecificationEvidence.objects.filter(
            product=product,
            field=item.field,
            status=SpecificationEvidence.Status.PENDING_REVIEW,
            value_normalized=fv.normalized,
        )
        .order_by("-observed_at")
        .first()
    )
    if pending is not None:
        pending.status = SpecificationEvidence.Status.REVIEW_APPROVED
        pending.is_current = True
        pending.note = f"approved by {actor}: {reason}"[:300]
        pending.save()
    else:
        SpecificationEvidence.objects.create(
            product=product,
            field=item.field,
            value_original=value,
            value_normalized=fv.normalized,
            unit_original=(fv.unit_original or "")[:40],
            unit_normalized=(fv.unit or "")[:40],
            source=item.source_record.source if item.source_record else None,
            source_record=item.source_record,
            source_url=item.source_record.canonical_url if item.source_record else "",
            locator=f"review:{item.pk}",
            note=f"set by reviewer {actor}: {reason}"[:300],
            observed_at=timezone.now(),
            status=SpecificationEvidence.Status.REVIEW_APPROVED,
        )
    product.specifications = candidate
    product.pending_review_fields = [f for f in product.pending_review_fields if f != item.field]
    product.save()
    refresh_derived_and_search(product)


def _clear_pending(item: ReviewItem) -> None:
    product = item.product
    if product is None or not item.field:
        return
    SpecificationEvidence.objects.filter(
        product=product, field=item.field, status=SpecificationEvidence.Status.PENDING_REVIEW
    ).update(status=SpecificationEvidence.Status.REJECTED)
    still_pending = (
        ReviewItem.objects.filter(product=product, field=item.field, status=ReviewItem.Status.PENDING)
        .exclude(pk=item.pk)
        .exists()
    )
    if not still_pending:
        product.pending_review_fields = [f for f in product.pending_review_fields if f != item.field]
        product.save(update_fields=["pending_review_fields", "updated_at"])


@transaction.atomic
def decide(
    item_id: int,
    *,
    approve: bool,
    actor: str,
    reason: str,
    value: Any = None,
    merge_into: str | None = None,
    create_new: bool = False,
) -> ReviewItem:
    if not actor.strip() or not reason.strip():
        raise ReviewError("actor and reason are mandatory for every review decision")
    item = ReviewItem.objects.select_for_update(of=("self",)).select_related("product", "source_record").get(pk=item_id)
    if item.status != ReviewItem.Status.PENDING:
        raise ReviewError(f"review item #{item.pk} is already {item.status}")

    kind = item.kind
    if approve:
        if kind == ReviewItem.Kind.NEW_PRODUCT:
            assert item.product is not None
            item.product.mark_published()
            item.product.save()
        elif kind in (ReviewItem.Kind.CRITICAL_CHANGE, ReviewItem.Kind.CONFLICT):
            if item.field in ("category", "variant.gtin"):
                raise ReviewError("structural conflicts must be fixed at the source or by re-import")
            _set_field(item, item.proposed_value if value is None else value, actor, reason)
        elif kind == ReviewItem.Kind.INVALID_VALUE:
            if value is None or item.field == "*":
                raise ReviewError("approving an invalid value requires --value with a corrected value")
            _set_field(item, value, actor, reason)
        elif kind == ReviewItem.Kind.MISSING_AT_SOURCE:
            assert item.product is not None
            item.product.publication_status = Product.PublicationStatus.WITHDRAWN
            item.product.save(update_fields=["publication_status", "updated_at"])
        elif kind == ReviewItem.Kind.AMBIGUOUS_MATCH:
            if bool(merge_into) == bool(create_new):
                raise ReviewError("ambiguous matches require exactly one of --merge-into or --create-new")
            extracted = ExtractedProduct.model_validate(item.payload["extracted"])
            source = Source.objects.get(slug=item.payload["source"])
            target = Product.objects.get(public_id=merge_into) if merge_into else None
            outcome = ingest_product(
                source, extracted, record=item.source_record, force_product=target, force_new=create_new
            )
            item.product = outcome.product
        else:  # pragma: no cover - exhaustive
            raise ReviewError(f"unsupported kind {kind}")
        item.status = ReviewItem.Status.APPROVED
    else:
        if kind == ReviewItem.Kind.NEW_PRODUCT and item.product is not None:
            item.product.publication_status = Product.PublicationStatus.WITHDRAWN
            item.product.save(update_fields=["publication_status", "updated_at"])
        _clear_pending(item)
        item.status = ReviewItem.Status.REJECTED
    item.decided_at = timezone.now()
    item.decided_by = actor.strip()[:120]
    item.decision_reason = reason.strip()
    item.save()
    return item
