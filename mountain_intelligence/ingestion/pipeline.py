"""Discover -> Fetch -> Extract -> Normalize -> Validate -> Match -> Review/Publish.

Idempotency: each (source, external_id) maps to one SourceRecord; the canonical hash of the
validated extraction contract is stored, so a repeated run with unchanged content does not
create products, variants or evidence. Products are never deleted by ingestion: errors, 404s
and absence from discovery only flag records/products for operator review.

Universal catalog: a source with ``categories: []`` accepts every product it publishes. Products
that match no technical category are stored as ``uncategorized`` with their original metadata
(no normalized specifications) and can be reclassified later (progressive enrichment). A source
with a category list only keeps products classified into those categories (``out_of_scope``).

Large catalogs: with ``config.batch_size`` each run processes the next window of the (sorted,
de-duplicated) discovery and stores a cursor in ``Source.sync_state``; the next run continues.
A run is only ``catalog_complete`` when discovery was complete and the cycle reached its end; a
truncated discovery never flags products as missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from mountain_intelligence.catalog.models import DataStatus, Product, ProductVariant
from mountain_intelligence.catalog.normalization import FieldValue, fold, normalize_specifications
from mountain_intelligence.catalog.services import (
    get_or_create_brand,
    make_public_id,
    refresh_derived_and_search,
)
from mountain_intelligence.catalog.taxonomy import CATEGORIES, UNCATEGORIZED_SLUG, CategoryDefinition
from mountain_intelligence.offers.models import Offer, PriceObservation
from mountain_intelligence.sources.models import Source, SourceRecord, SpecificationEvidence

from .adapters.base import AdapterContext, AdapterError, canonical_key, get_adapter, limited
from .contracts import DiscoveredItem, ExtractedProduct, SourceAttribute
from .http import FetchError, SafeFetcher
from .models import IngestionRun, ReviewItem

logger = logging.getLogger(__name__)

MAX_RUN_ERRORS = 200
MAX_BATCH_SIZE = 20_000


class SourceNotSyncable(Exception):
    pass


@dataclass
class ItemOutcome:
    status: str  # new | updated | unchanged | not_modified | conflict | invalid | out_of_scope | error
    product: Product | None = None
    reviews: int = 0
    messages: list[str] = field(default_factory=list)


def payload_hash(extracted: ExtractedProduct) -> str:
    data = extracted.model_dump(mode="json", exclude={"offers"})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evidence_status_for(source: Source) -> str:
    if source.is_synthetic:
        return SpecificationEvidence.Status.SYNTHETIC_FIXTURE
    if source.source_type == Source.SourceType.STRUCTURED_FILE:
        return SpecificationEvidence.Status.MANUAL_IMPORT
    return SpecificationEvidence.Status.SOURCE_VERIFIED


def data_status_for(source: Source) -> str:
    if source.is_synthetic:
        return DataStatus.SYNTHETIC_FIXTURE
    if source.source_type == Source.SourceType.STRUCTURED_FILE:
        return DataStatus.MANUAL_ENTRY
    return DataStatus.SOURCE_EXTRACTED


# ------------------------------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------------------------------


def _norm_name(name: str, brand_name: str) -> str:
    folded = " ".join(fold(name).split())
    brand = fold(brand_name)
    if folded.startswith(brand + " "):
        folded = folded[len(brand) + 1 :]
    return folded


@dataclass
class MatchResult:
    product: Product | None
    ambiguous: list[Product] = field(default_factory=list)
    similar: list[Product] = field(default_factory=list)
    reason: str = ""


def match_product(
    extracted: ExtractedProduct,
    record: SourceRecord | None,
    brand_id: int | None,
    brand_name: str,
    category: CategoryDefinition,
) -> MatchResult:
    if record is not None and record.product_id:
        return MatchResult(record.product, reason="source_record")
    gtins = [v.gtin for v in extracted.variants if v.gtin]
    if gtins:
        hits = list(Product.objects.filter(variants__gtin__in=gtins).distinct())
        if len(hits) == 1 and hits[0].brand_id == brand_id:
            return MatchResult(hits[0], reason="gtin")
        if hits:
            return MatchResult(None, ambiguous=hits, reason="gtin matches a different brand or several products")
    if brand_id is None:
        return MatchResult(None)
    if extracted.mpn:
        hit = Product.objects.filter(brand_id=brand_id, mpn__iexact=extracted.mpn).first()
        if hit:
            return MatchResult(hit, reason="brand+mpn")
    target = _norm_name(extracted.name, brand_name)
    candidates = _match_scope(Product.objects.filter(brand_id=brand_id), category)
    same_name = [p for p in candidates if _norm_name(p.name, brand_name) == target]
    exact = [p for p in same_name if fold(p.generation) == fold(extracted.generation)]
    if len(exact) == 1:
        return MatchResult(exact[0], reason="brand+name+generation")
    if exact:
        return MatchResult(None, ambiguous=exact, reason="several products share name and generation")
    # Same name, one side without generation: cannot tell whether it is the same model.
    unknown_gen = [p for p in same_name if not p.generation or not extracted.generation]
    if unknown_gen:
        return MatchResult(None, ambiguous=unknown_gen, reason="same name but generation unknown on one side")
    similar = list(
        _match_scope(Product.objects.filter(brand_id=brand_id), category)
        .annotate(similarity=TrigramSimilarity(Lower("name"), target))
        .filter(similarity__gt=0.6)
        .order_by("-similarity")[:5]
    )
    # Same name with a *different, explicit* generation is a distinct model: not a duplicate.
    return MatchResult(None, similar=[p for p in similar if p not in same_name])


def _match_scope(qs, category: CategoryDefinition):
    """Products a name match may target.

    A technical classification also matches the brand's `uncategorized` products (so they get
    upgraded instead of duplicated); an `uncategorized` extraction matches any category (it never
    creates a second copy of a model that is already classified).
    """
    if not category.technical:
        return qs
    return qs.filter(category__slug__in=[category.slug, UNCATEGORIZED_SLUG])


# ------------------------------------------------------------------------------------------
# Evidence helpers
# ------------------------------------------------------------------------------------------


def _current_evidence(product: Product, field_name: str) -> SpecificationEvidence | None:
    return (
        SpecificationEvidence.objects.filter(product=product, field=field_name, is_current=True, variant=None)
        .order_by("-observed_at")
        .first()
    )


def _add_evidence(
    product: Product,
    fv: FieldValue,
    *,
    source: Source,
    record: SourceRecord | None,
    extracted: ExtractedProduct,
    observed_at: datetime,
    status: str,
    is_current: bool = True,
    note: str = "",
) -> SpecificationEvidence:
    loc = extracted.locators.get(fv.field)
    return SpecificationEvidence.objects.create(
        product=product,
        field=fv.field,
        value_original=_jsonable(fv.original),
        value_normalized=fv.normalized,
        unit_original=(fv.unit_original or "")[:40],
        unit_normalized=(fv.unit or "")[:40],
        source=source,
        source_record=record,
        source_url=extracted.canonical_url,
        locator=(loc.locator if loc else f"field:{fv.field}")[:300],
        excerpt=(loc.excerpt if loc else "")[:280],
        note=(note or fv.note or "")[:300],
        observed_at=observed_at,
        status=status,
        is_current=is_current,
    )


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _open_review(
    *,
    kind: str,
    reason: str,
    product: Product | None = None,
    record: SourceRecord | None = None,
    run: IngestionRun | None = None,
    field_name: str = "",
    current: Any = None,
    proposed: Any = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Create a review item unless an identical pending one already exists (idempotent)."""
    same_proposal = Q(proposed_value__isnull=True) if proposed is None else Q(proposed_value=proposed)
    exists = ReviewItem.objects.filter(
        same_proposal,
        kind=kind,
        status=ReviewItem.Status.PENDING,
        product=product,
        field=field_name,
        source_record=record,
    ).exists()
    if exists:
        return False
    if field_name and product is not None:
        # A newer observation for the same field supersedes older pending proposals.
        ReviewItem.objects.filter(
            kind=kind, status=ReviewItem.Status.PENDING, product=product, field=field_name, source_record=record
        ).update(
            status=ReviewItem.Status.OBSOLETE,
            decided_at=timezone.now(),
            decided_by="system",
            decision_reason="superseded by a newer observation",
        )
    ReviewItem.objects.create(
        kind=kind,
        reason=reason,
        product=product,
        source_record=record,
        ingestion_run=run,
        field=field_name,
        current_value=current,
        proposed_value=proposed,
        payload=payload or {},
    )
    return True


# ------------------------------------------------------------------------------------------
# Item ingestion
# ------------------------------------------------------------------------------------------


def ingest_product(
    source: Source,
    extracted: ExtractedProduct,
    *,
    run: IngestionRun | None = None,
    record: SourceRecord | None = None,
    observed_at: datetime | None = None,
    force_product: Product | None = None,
    force_new: bool = False,
) -> ItemOutcome:
    """Normalize, validate, match and persist one extracted product. Call inside a transaction."""
    observed_at = observed_at or timezone.now()
    category = CATEGORIES.get(extracted.category)
    if category is None:
        return ItemOutcome("invalid", messages=[f"unknown category {extracted.category!r}"])
    if force_product is None and not source.accepts_category(category.slug):
        return ItemOutcome(
            "out_of_scope",
            messages=[f"category {category.slug!r} is outside the source scope {sorted(source.categories)}"],
        )
    if not category.technical:
        extracted = _specs_to_source_attributes(extracted)

    explicit_units = {k: loc.unit for k, loc in extracted.locators.items() if loc.unit}
    norm = normalize_specifications(
        category,
        extracted.specifications,
        units_from_field_names=extracted.units_from_field_names,
        explicit_units=explicit_units,
    )
    digest = payload_hash(extracted)
    if record is None:
        record, _ = SourceRecord.objects.get_or_create(
            source=source,
            external_id=extracted.external_id,
            defaults={"canonical_url": extracted.canonical_url[:1000]},
        )
    record.last_seen_at = observed_at
    record.status = SourceRecord.Status.ACTIVE
    if record.content_hash == digest and record.product_id and not force_product and not force_new:
        record.save(update_fields=["last_seen_at", "status"])
        Product.objects.filter(pk=record.product_id).update(
            last_verified_at=observed_at, source_status=Product.SourceStatus.OK, source_status_since=None
        )
        _upsert_offers(record.product, extracted, source, observed_at)
        return ItemOutcome("unchanged", record.product)

    brand = get_or_create_brand(
        extracted.brand, official_domain=extracted.brand_domain, is_synthetic=source.is_synthetic
    )
    if force_product is not None:
        match = MatchResult(force_product, reason="reviewer decision")
    elif force_new:
        match = MatchResult(None)
    else:
        match = match_product(extracted, record, brand.id, brand.name, category)

    outcome: ItemOutcome
    if match.ambiguous:
        created = _open_review(
            kind=ReviewItem.Kind.AMBIGUOUS_MATCH,
            record=record,
            run=run,
            reason=f"Ambiguous match for {extracted.brand} {extracted.name}: {match.reason}",
            payload={
                "extracted": extracted.model_dump(mode="json"),
                "source": source.slug,
                "candidates": [p.public_id for p in match.ambiguous],
            },
            proposed={"hash": digest},
        )
        record.extracted_payload = extracted.model_dump(mode="json")
        record.save()
        return ItemOutcome("conflict", reviews=int(created), messages=[match.reason])

    if match.product is None:
        outcome = _create_product(source, extracted, brand, category, norm, record, run, observed_at, match)
    else:
        outcome = _update_product(source, extracted, match.product, category, norm, record, run, observed_at)

    product = outcome.product
    assert product is not None
    for issue in norm.issues:
        created = _open_review(
            kind=ReviewItem.Kind.INVALID_VALUE,
            product=product,
            record=record,
            run=run,
            field_name=issue.field[:80],
            current=None,
            proposed=_jsonable(issue.original),
            reason=f"{issue.code}: {issue.message}",
        )
        outcome.reviews += int(created)
    _upsert_variants(product, extracted, category)
    _upsert_offers(product, extracted, source, observed_at)

    record.product = product
    record.content_hash = digest
    record.canonical_url = extracted.canonical_url[:1000]
    record.extracted_payload = extracted.model_dump(mode="json")
    record.last_changed_at = observed_at
    record.save()
    refresh_derived_and_search(product)
    if norm.issues and outcome.status in {"unchanged"}:
        outcome.status = "updated"
    return outcome


def _specs_to_source_attributes(extracted: ExtractedProduct) -> ExtractedProduct:
    """Without a technical schema nothing is normalized: raw values are kept as original attributes."""
    if not extracted.specifications:
        return extracted
    moved = []
    for name, value in extracted.specifications.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        loc = extracted.locators.get(name)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        moved.append(
            SourceAttribute(
                name=name[:120],
                value=" ".join(text.split())[:500] or "-",
                unit=((loc.unit or "") if loc else "")[:20],
                locator=(loc.locator if loc else f"field:{name}")[:300],
            )
        )
    known = {(a.name, a.value) for a in extracted.source_attributes}
    merged = [*extracted.source_attributes, *(a for a in moved if (a.name, a.value) not in known)][:150]
    return extracted.model_copy(update={"specifications": {}, "locators": {}, "source_attributes": merged})


def _apply_metadata(product: Product, extracted: ExtractedProduct, source: Source) -> bool:
    """Store the manufacturer's original catalog metadata. Non-empty observations replace older
    values; empty ones never erase what another observation provided. Returns True on change."""
    before = (
        product.source_category,
        product.product_type,
        list(product.tags),
        list(product.collections),
        dict(product.external_ids or {}),
        list(product.images or []),
        list(product.source_attributes or []),
    )
    if extracted.source_category:
        product.source_category = extracted.source_category
    if extracted.product_type:
        product.product_type = extracted.product_type
    if extracted.tags:
        product.tags = extracted.tags
    if extracted.collections:
        product.collections = extracted.collections
    if extracted.external_ids:
        product.external_ids = {**(product.external_ids or {}), source.slug: extracted.external_ids}
    if extracted.images:
        product.images = [img.model_dump(mode="json") for img in extracted.images]
    if extracted.source_attributes:
        others = [a for a in product.source_attributes or [] if isinstance(a, dict) and a.get("source") != source.slug]
        mine = [{**a.model_dump(mode="json"), "source": source.slug} for a in extracted.source_attributes]
        product.source_attributes = (mine + others)[:300]
    after = (
        product.source_category,
        product.product_type,
        list(product.tags),
        list(product.collections),
        dict(product.external_ids or {}),
        list(product.images or []),
        list(product.source_attributes or []),
    )
    return before != after


def _create_product(
    source: Source,
    extracted: ExtractedProduct,
    brand,
    category: CategoryDefinition,
    norm,
    record: SourceRecord,
    run: IngestionRun | None,
    observed_at: datetime,
    match: MatchResult,
) -> ItemOutcome:
    from mountain_intelligence.catalog.models import Category

    product = Product(
        public_id=make_public_id(brand, extracted.name, extracted.generation),
        brand=brand,
        category=Category.objects.get(slug=category.slug),
        name=extracted.name,
        generation=extracted.generation,
        model_year=extracted.model_year,
        mpn=extracted.mpn,
        aliases=extracted.aliases,
        summary=extracted.summary,
        specifications=norm.specifications,
        data_status=data_status_for(source),
        last_verified_at=observed_at,
        spec_schema_version=category.schema_version,
        category_basis=extracted.category_basis,
    )
    _apply_metadata(product, extracted, source)
    possible_duplicates = [p.public_id for p in match.similar]
    publish_now = source.auto_publish and not possible_duplicates
    if publish_now:
        product.mark_published()
    else:
        product.publication_status = Product.PublicationStatus.IN_REVIEW
    product.save()
    status = evidence_status_for(source)
    for fv in norm.values.values():
        _add_evidence(
            product, fv, source=source, record=record, extracted=extracted, observed_at=observed_at, status=status
        )
    reviews = 0
    if not publish_now:
        reason = "New product awaiting review before publication"
        if possible_duplicates:
            reason += f"; possible duplicates (never merged automatically): {', '.join(possible_duplicates)}"
        reviews += int(
            _open_review(
                kind=ReviewItem.Kind.NEW_PRODUCT,
                product=product,
                record=record,
                run=run,
                reason=reason,
                payload={"possible_duplicates": possible_duplicates},
            )
        )
    return ItemOutcome("new", product, reviews)


def _update_product(
    source: Source,
    extracted: ExtractedProduct,
    product: Product,
    category: CategoryDefinition,
    norm,
    record: SourceRecord,
    run: IngestionRun | None,
    observed_at: datetime,
) -> ItemOutcome:
    reclassified = False
    if product.category.slug != category.slug and not category.technical:
        # Never downgrade: a product already in a technical category keeps it (and its specs).
        category = CATEGORIES[product.category.slug]
    elif product.category.slug == UNCATEGORIZED_SLUG and category.technical:
        # Progressive enrichment: an unclassified product gains a technical category.
        from mountain_intelligence.catalog.models import Category

        clash = (
            Product.objects.filter(
                brand_id=product.brand_id,
                category__slug=category.slug,
                name=product.name,
                generation=product.generation,
            )
            .exclude(pk=product.pk)
            .first()
        )
        if clash is not None:
            created = _open_review(
                kind=ReviewItem.Kind.CONFLICT,
                product=product,
                record=record,
                run=run,
                field_name="category",
                current=product.category.slug,
                proposed=category.slug,
                reason=f"Reclassification would duplicate {clash.public_id}; merge or reject manually",
            )
            return ItemOutcome("conflict", product, int(created))
        product.category = Category.objects.get(slug=category.slug)
        product.category_basis = extracted.category_basis
        product.spec_schema_version = category.schema_version
        reclassified = True
    if product.category.slug == category.slug and not product.category_basis and extracted.category == category.slug:
        product.category_basis = extracted.category_basis  # products created before basis tracking
    if product.category.slug != category.slug:
        created = _open_review(
            kind=ReviewItem.Kind.CONFLICT,
            product=product,
            record=record,
            run=run,
            field_name="category",
            current=product.category.slug,
            proposed=category.slug,
            reason="Source reports a different category for a matched product",
        )
        return ItemOutcome("conflict", product, int(created))
    specs = dict(product.specifications or {})
    status = evidence_status_for(source)
    changed, reviews, conflict = False, 0, False
    published = product.publication_status == Product.PublicationStatus.PUBLISHED
    pending = set(product.pending_review_fields)
    for name, fv in norm.values.items():
        spec = category.field_map[name]
        current_value = specs.get(name)
        current_ev = _current_evidence(product, name)
        if current_value == fv.normalized:
            if current_ev and current_ev.source_id == source.id:
                SpecificationEvidence.objects.filter(pk=current_ev.pk).update(observed_at=observed_at)
            else:
                _add_evidence(
                    product,
                    fv,
                    source=source,
                    record=record,
                    extracted=extracted,
                    observed_at=observed_at,
                    status=status,
                    note="corroborates existing value",
                )
            continue
        other_source = current_value is not None and current_ev is not None and current_ev.source_id != source.id
        needs_review = (spec.critical and published) or other_source
        if needs_review:
            _add_evidence(
                product,
                fv,
                source=source,
                record=record,
                extracted=extracted,
                observed_at=observed_at,
                status=SpecificationEvidence.Status.PENDING_REVIEW,
                is_current=False,
            )
            kind = ReviewItem.Kind.CONFLICT if other_source else ReviewItem.Kind.CRITICAL_CHANGE
            reason = (
                "Value differs from another source's current evidence"
                if other_source
                else "Change to a critical (safety/compatibility) field requires review"
            )
            reviews += int(
                _open_review(
                    kind=kind,
                    product=product,
                    record=record,
                    run=run,
                    field_name=name,
                    current=current_value,
                    proposed=fv.normalized,
                    reason=reason,
                    payload={"source": source.slug, "unit": fv.unit, "original": _jsonable(fv.original)},
                )
            )
            pending.add(name)
            conflict = conflict or other_source
            continue
        SpecificationEvidence.objects.filter(product=product, field=name, is_current=True, variant=None).update(
            is_current=False, status=SpecificationEvidence.Status.SUPERSEDED
        )
        _add_evidence(
            product, fv, source=source, record=record, extracted=extracted, observed_at=observed_at, status=status
        )
        specs[name] = fv.normalized
        changed = True
    for attr in ("mpn", "generation", "model_year"):
        new = getattr(extracted, attr)
        if new and not getattr(product, attr):
            setattr(product, attr, new)
            changed = True
    changed = _apply_metadata(product, extracted, source) or changed or reclassified
    product.specifications = specs
    product.pending_review_fields = sorted(pending)
    product.last_verified_at = observed_at
    product.source_status = Product.SourceStatus.OK
    product.source_status_since = None
    product.save()
    if conflict:
        return ItemOutcome("conflict", product, reviews)
    return ItemOutcome("updated" if changed or reviews else "unchanged", product, reviews)


def _upsert_variants(product: Product, extracted: ExtractedProduct, category: CategoryDefinition) -> None:
    for v in extracted.variants:
        lookup = Q(product=product)
        if v.gtin:
            existing = ProductVariant.objects.filter(gtin=v.gtin).first()
            if existing and existing.product_id != product.id:
                _open_review(
                    kind=ReviewItem.Kind.CONFLICT,
                    product=product,
                    field_name="variant.gtin",
                    proposed=v.gtin,
                    current=existing.product.public_id,
                    reason="GTIN already assigned to another product",
                )
                continue
            lookup &= Q(gtin=v.gtin)
        elif v.sku:
            lookup &= Q(sku=v.sku)
        elif v.external_id:
            lookup &= Q(external_id=v.external_id, sku="", gtin="")
        else:
            lookup &= Q(size_label=v.size_label, color=v.color, sku="", gtin="")
        overrides = {}
        if v.spec_overrides:
            norm = normalize_specifications(
                category, v.spec_overrides, units_from_field_names=extracted.units_from_field_names
            )
            overrides = norm.specifications
        variant = ProductVariant.objects.filter(lookup).first() or ProductVariant(product=product)
        variant.sku, variant.mpn, variant.gtin = v.sku, v.mpn, v.gtin
        variant.size_label, variant.size_eu, variant.color = v.size_label, v.size_eu, v.color
        variant.external_id, variant.options, variant.image_url = v.external_id, v.options, v.image_url
        variant.spec_overrides = overrides
        variant.save()


def _upsert_offers(product: Product | None, extracted: ExtractedProduct, source: Source, observed_at: datetime) -> None:
    if product is None or not extracted.offers:
        return
    allowed = (
        source.is_synthetic
        or source.source_type in (Source.SourceType.DISTRIBUTOR_FEED, Source.SourceType.STRUCTURED_FILE)
        or source.config.get("capture_offers")
    )
    if not allowed:
        return
    for o in extracted.offers:
        variant = product.variants.filter(sku=o.variant_sku).first() if o.variant_sku else None
        when = o.observed_at or observed_at
        offer, _ = Offer.objects.update_or_create(
            product=product,
            variant=variant,
            retailer=o.retailer,
            defaults={
                "source": source,
                "url": o.url,
                "currency": o.currency,
                "price": o.price,
                "availability": o.availability,
                "observed_at": when,
                "data_status": data_status_for(source),
            },
        )
        PriceObservation.objects.get_or_create(
            offer=offer,
            observed_at=when,
            defaults={"price": o.price, "currency": o.currency, "availability": o.availability},
        )


# ------------------------------------------------------------------------------------------
# Runs
# ------------------------------------------------------------------------------------------


@contextmanager
def source_lock(source: Source):
    key = f"ingestion-lock:{source.slug}"
    acquired = cache.add(key, "1", timeout=settings.INGESTION_LOCK_TTL_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


def run_source_sync(
    source: Source,
    *,
    trigger: str = IngestionRun.Trigger.CLI,
    actor: str = "",
    dry_run: bool = False,
    max_items: int | None = None,
    fetcher: SafeFetcher | None = None,
    ignore_enabled: bool = False,
    batch_size: int | None = None,
    restart_cycle: bool = False,
) -> IngestionRun:
    """Run one ingestion for `source`. Never raises for item-level problems.

    ``max_items`` processes at most N items (validation/debugging; never a complete sync).
    ``batch_size`` (or ``config.batch_size``) processes the next window of a multi-run cycle.
    """
    if not source.can_sync and not (ignore_enabled and source.access_status in Source.ACCESS_ALLOWING_SYNC):
        raise SourceNotSyncable(
            f"source {source.slug!r} is not syncable (enabled={source.enabled}, access={source.access_status})"
        )
    adapter = get_adapter(source.adapter)
    run = IngestionRun.objects.create(source=source, trigger=trigger, dry_run=dry_run, actor=actor[:120])
    metrics = dict.fromkeys(
        (
            "discovered",
            "processed",
            "new",
            "updated",
            "unchanged",
            "not_modified",
            "conflicts",
            "invalid",
            "out_of_scope",
            "errors",
            "missing",
            "reactivated",
            "rekeyed",
            "reviews_opened",
        ),
        0,
    )
    errors: list[dict[str, str]] = []

    def record_error(external_id: str, kind: str, message: str) -> None:
        metrics["errors"] += 1
        if len(errors) < MAX_RUN_ERRORS:
            errors.append({"item": external_id[:300], "kind": kind, "message": message[:300]})

    if batch_size is None and source.config.get("batch_size"):
        batch_size = int(source.config["batch_size"])
    if batch_size is not None:
        batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))

    with source_lock(source) as acquired:
        if not acquired:
            run.status = IngestionRun.Status.SKIPPED
            run.errors = [{"item": "*", "kind": "locked", "message": "another run holds the source lock"}]
            run.finished_at = timezone.now()
            run.save()
            return run
        own_fetcher = fetcher is None and adapter.needs_network
        if own_fetcher:
            fetcher = SafeFetcher(source.allowed_domains)
        ctx = AdapterContext(source=source, fetcher=fetcher, max_items=max_items)
        discovery_ok = False
        cycle_done = False
        try:
            if max_items is not None:
                # Bounded validation run: lazy discovery, never a complete synchronization.
                items = _unique(limited(adapter.discover(ctx), max_items))
                all_keys = {i.external_id for i in items}
                window, cycle_done = items, False
            else:
                items = _unique(adapter.discover(ctx))
                all_keys = {i.external_id for i in items}
                discovery_ok = True
                window, cycle_done = _batch_window(source, items, batch_size, run, restart_cycle)
            metrics["discovered"] = len(items)
            if discovery_ok and not dry_run:
                metrics["rekeyed"] = _rekey_legacy_records(source, adapter, all_keys)
                if _discovery_is_complete(ctx):
                    metrics["reactivated"] = _reactivate_rediscovered(source, all_keys)
            for item in window:
                metrics["processed"] += 1
                _process_item(source, adapter, ctx, item, run, metrics, record_error, dry_run)
        except (AdapterError, FetchError) as exc:
            record_error("*discovery*", getattr(exc, "kind", "adapter"), str(exc))
            run.status = IngestionRun.Status.FAILED
            discovery_ok = False
        finally:
            if own_fetcher and fetcher is not None:
                fetcher.close()
        failed = run.status == IngestionRun.Status.FAILED
        discovery_complete = discovery_ok and not failed and _discovery_is_complete(ctx)
        if discovery_complete and not dry_run:
            # Only a complete discovery may decide that something disappeared from the source.
            metrics["missing"] = _flag_missing(source, all_keys, run)
        if not failed and not dry_run and batch_size is not None and max_items is None:
            _advance_cycle(source, run, cycle_done=cycle_done, discovery_complete=discovery_complete)
    run.discovery_complete = discovery_complete
    run.catalog_complete = discovery_complete and cycle_done and max_items is None
    if run.status != IngestionRun.Status.FAILED:
        if metrics["errors"]:
            run.status = IngestionRun.Status.PARTIAL
        elif not run.catalog_complete:
            run.status = IngestionRun.Status.INCOMPLETE
        else:
            run.status = IngestionRun.Status.SUCCEEDED
    warnings: list[str] = []
    if max_items is not None:
        warnings.append("bounded run (--max-items): not a complete sync; missing-product detection skipped")
    elif not discovery_complete and not failed:
        warnings.append("discovery incomplete: missing-product detection skipped; run is not a complete sync")
    warnings.extend(dict.fromkeys(ctx.warnings))  # completeness first, then unique adapter warnings
    run.metrics = {**metrics, "warnings": warnings[:20]}
    run.errors = errors
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "ingestion run finished",
        extra={
            "run_id": str(run.run_id),
            "source": source.slug,
            "status": run.status,
            "metrics": metrics,
            "catalog_complete": run.catalog_complete,
        },
    )
    return run


def _discovery_is_complete(ctx: AdapterContext) -> bool:
    """Adapters report truncation through `ctx.discovery_complete` (see ingestion.discovery)."""
    return ctx.discovery_complete


def _unique(items) -> list[DiscoveredItem]:
    """De-duplicate discovered items by identity, keeping the first occurrence."""
    out: dict[str, DiscoveredItem] = {}
    for item in items:
        out.setdefault(item.external_id, item)
    return list(out.values())


def _batch_window(
    source: Source, items: list[DiscoveredItem], batch_size: int | None, run: IngestionRun, restart: bool
) -> tuple[list[DiscoveredItem], bool]:
    """Select the items this run processes. Returns (window, cycle_done)."""
    if batch_size is None:
        run.batch = {"mode": "full", "total": len(items)}
        return items, True
    state = {} if restart else dict(source.sync_state or {})
    cursor = str(state.get("cursor") or "")
    ordered = sorted(items, key=lambda i: i.external_id)
    pending = [i for i in ordered if i.external_id > cursor] if cursor else ordered
    window = pending[:batch_size]
    run.batch = {
        "mode": "batch",
        "cycle_id": state.get("cycle_id") or str(run.run_id),
        "cycle_started_at": state.get("cycle_started_at") or run.started_at.isoformat(),
        "batch_size": batch_size,
        "total": len(ordered),
        "already_done": len(ordered) - len(pending),
        "this_batch": len(window),
        "remaining_after": max(0, len(pending) - len(window)),
        "cursor_from": cursor or None,
        "cursor_to": window[-1].external_id if window else cursor or None,
    }
    return window, len(pending) <= batch_size


def _advance_cycle(source: Source, run: IngestionRun, *, cycle_done: bool, discovery_complete: bool) -> None:
    state = dict(source.sync_state or {})
    batch = run.batch or {}
    if cycle_done:
        state = {
            "cycles_completed": int(state.get("cycles_completed", 0)) + 1,
            "last_cycle_id": batch.get("cycle_id"),
            "last_cycle_finished_at": timezone.now().isoformat(),
            "last_cycle_complete": discovery_complete,
        }
    else:
        state.update(
            cycle_id=batch.get("cycle_id"),
            cycle_started_at=batch.get("cycle_started_at"),
            cursor=batch.get("cursor_to"),
            updated_at=timezone.now().isoformat(),
        )
    source.sync_state = state
    Source.objects.filter(pk=source.pk).update(sync_state=state)


def cycle_in_progress(source: Source) -> bool:
    return bool((source.sync_state or {}).get("cursor"))


def _rekey_legacy_records(source: Source, adapter, discovered: set[str]) -> int:
    """Move records stored under a non-canonical id (older URL forms) to their canonical identity.

    Keeps product links, ETags and history when URL canonicalization changes, instead of flagging
    the old record as missing and creating a new one.
    """
    count = 0
    stale = SourceRecord.objects.filter(source=source).exclude(external_id__in=discovered)
    existing = set(
        SourceRecord.objects.filter(source=source, external_id__in=discovered).values_list("external_id", flat=True)
    )
    for record in stale.only("id", "external_id"):
        key = canonical_key(adapter, record.external_id, source.config)
        if key != record.external_id and key in discovered and key not in existing:
            SourceRecord.objects.filter(pk=record.pk).update(external_id=key[:300])
            existing.add(key)
            count += 1
    return count


def _reactivate_rediscovered(source: Source, discovered: set[str]) -> int:
    """Records flagged missing that are listed again by a complete discovery become active."""
    records = list(
        SourceRecord.objects.filter(source=source, status=SourceRecord.Status.MISSING, external_id__in=discovered)
    )
    for record in records:
        SourceRecord.objects.filter(pk=record.pk).update(status=SourceRecord.Status.ACTIVE)
        if record.product_id:
            Product.objects.filter(pk=record.product_id, source_status=Product.SourceStatus.MISSING_AT_SOURCE).update(
                source_status=Product.SourceStatus.OK, source_status_since=None
            )
            ReviewItem.objects.filter(
                kind=ReviewItem.Kind.MISSING_AT_SOURCE, status=ReviewItem.Status.PENDING, source_record=record
            ).update(
                status=ReviewItem.Status.OBSOLETE,
                decided_at=timezone.now(),
                decided_by="system",
                decision_reason="listed again by the source",
            )
    return len(records)


def _process_item(source, adapter, ctx, item, run, metrics, record_error, dry_run) -> None:
    record = SourceRecord.objects.filter(source=source, external_id=item.external_id).first()
    try:
        document = adapter.fetch(
            item, ctx, etag=record.etag if record else "", last_modified=record.last_modified if record else ""
        )
    except FetchError as exc:
        record_error(item.external_id, exc.kind, str(exc))
        if record and not dry_run:
            now = timezone.now()
            record.status = SourceRecord.Status.GONE if exc.kind == "not_found" else SourceRecord.Status.ERROR
            record.last_error = str(exc)[:500]
            record.http_status = exc.status_code
            record.save()
            if record.product_id:
                new_status = (
                    Product.SourceStatus.MISSING_AT_SOURCE
                    if exc.kind == "not_found"
                    else Product.SourceStatus.FETCH_ERROR
                )
                Product.objects.filter(pk=record.product_id).exclude(source_status=new_status).update(
                    source_status=new_status, source_status_since=now
                )
        return
    if document.not_modified and record is not None:
        metrics["not_modified"] += 1
        if not dry_run:
            record.last_seen_at = record.last_fetched_at = document.fetched_at
            record.status = SourceRecord.Status.ACTIVE
            record.save(update_fields=["last_seen_at", "last_fetched_at", "status"])
            if record.product_id:
                Product.objects.filter(pk=record.product_id).update(
                    last_verified_at=document.fetched_at, source_status=Product.SourceStatus.OK
                )
        return
    try:
        extracted_items = adapter.extract(document, ctx)
    except AdapterError as exc:
        metrics["invalid"] += 1
        record_error(item.external_id, "extraction", str(exc))
        return
    single = len(extracted_items) == 1
    for extracted in extracted_items:
        # One document -> one product: the record is keyed by the discovered item (URL/feed id), so
        # conditional requests and missing-item detection line up with discovery.
        record_key = item.external_id if single else extracted.external_id
        try:
            with transaction.atomic():
                item_record = (
                    record
                    if single and record is not None
                    else SourceRecord.objects.filter(source=source, external_id=record_key).first()
                )
                if item_record is None:
                    item_record = SourceRecord.objects.create(
                        source=source, external_id=record_key[:300], canonical_url=extracted.canonical_url[:1000]
                    )
                outcome = ingest_product(
                    source, extracted, run=run, record=item_record, observed_at=document.fetched_at
                )
                if outcome.status == "out_of_scope" and item_record.product_id is None:
                    # Nothing stored for an out-of-scope item: drop the empty record again.
                    SourceRecord.objects.filter(pk=item_record.pk).delete()
                if outcome.product is not None and outcome.status != "invalid":
                    SourceRecord.objects.filter(pk=item_record.pk).update(
                        etag=document.etag,
                        last_modified=document.last_modified,
                        http_status=document.status_code,
                        last_fetched_at=document.fetched_at,
                        last_error="",
                    )
                if dry_run:
                    transaction.set_rollback(True)
        except Exception as exc:  # item isolation: one bad item never aborts the run
            logger.exception("item ingestion failed", extra={"source": source.slug, "item": item.external_id})
            record_error(item.external_id, "internal", type(exc).__name__)
            continue
        key = {
            "new": "new",
            "updated": "updated",
            "unchanged": "unchanged",
            "conflict": "conflicts",
            "invalid": "invalid",
            "out_of_scope": "out_of_scope",
        }.get(outcome.status, "errors")
        metrics[key] += 1
        metrics["reviews_opened"] += outcome.reviews
        if outcome.status == "invalid":
            record_error(item.external_id, "invalid", "; ".join(outcome.messages))
            metrics["errors"] -= 1  # counted as invalid, not as an error


def _flag_missing(source: Source, seen: set[str], run: IngestionRun) -> int:
    missing = SourceRecord.objects.filter(source=source, status=SourceRecord.Status.ACTIVE).exclude(
        external_id__in=seen
    )
    count = 0
    now = timezone.now()
    for record in missing.select_related("product"):
        record.status = SourceRecord.Status.MISSING
        record.save(update_fields=["status"])
        count += 1
        if record.product_id:
            Product.objects.filter(pk=record.product_id).update(
                source_status=Product.SourceStatus.MISSING_AT_SOURCE, source_status_since=now
            )
            _open_review(
                kind=ReviewItem.Kind.MISSING_AT_SOURCE,
                product=record.product,
                record=record,
                run=run,
                reason="Product no longer discovered at the source. Approve to withdraw it from the "
                "catalog, reject to keep it published (e.g. temporary absence).",
            )
    return count
