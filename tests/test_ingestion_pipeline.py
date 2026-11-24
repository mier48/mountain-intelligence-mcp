"""Structured ingestion: idempotency, change tracking, review queue and regression cases."""

from __future__ import annotations

import pytest

from mountain_intelligence.catalog.models import Product, ProductVariant
from mountain_intelligence.ingestion.models import IngestionRun, ReviewItem
from mountain_intelligence.ingestion.pipeline import SourceNotSyncable, run_source_sync
from mountain_intelligence.ingestion.review import ReviewError, decide
from mountain_intelligence.sources.models import Source, SourceRecord, SpecificationEvidence

from .conftest import boot, write_products

pytestmark = pytest.mark.django_db


def sync(source):
    run = run_source_sync(source, trigger=IngestionRun.Trigger.CLI)
    source.refresh_from_db()
    return run


def test_repeated_ingestion_is_idempotent(structured_source):
    variants = [{"sku": "NW-42", "gtin": "4006381333931", "size_eu": 42}, {"sku": "NW-43", "size_eu": 43}]
    source = structured_source(products=[boot(variants=variants)])
    first = sync(source)
    assert first.metrics["new"] == 1 and first.status == IngestionRun.Status.SUCCEEDED
    counts = (Product.objects.count(), ProductVariant.objects.count(), SpecificationEvidence.objects.count())
    second = sync(source)
    assert second.metrics["unchanged"] == 1 and second.metrics["new"] == 0
    assert (Product.objects.count(), ProductVariant.objects.count(), SpecificationEvidence.objects.count()) == counts
    product = Product.objects.get()
    assert product.publication_status == Product.PublicationStatus.PUBLISHED
    assert product.derived_specifications == {"weight_per_boot_g": 800.0}
    assert SourceRecord.objects.get().product == product


def test_non_critical_change_updates_and_keeps_history(structured_source, import_dir):
    source = structured_source(products=[boot()])
    sync(source)
    changed = boot(specifications={**boot()["specifications"], "weight_g": 820})
    write_products(import_dir / "authorized-import.json", [changed])
    run = sync(source)
    assert run.metrics["updated"] == 1
    product = Product.objects.get()
    assert product.specifications["weight_g"] == 820
    history = SpecificationEvidence.objects.filter(product=product, field="weight_g").order_by("created_at")
    assert [(e.value_normalized, e.is_current, e.status) for e in history] == [
        (800.0, False, SpecificationEvidence.Status.SUPERSEDED),
        (820.0, True, SpecificationEvidence.Status.MANUAL_IMPORT),
    ]


def test_critical_change_goes_to_review_and_is_not_applied(structured_source, import_dir):
    source = structured_source(products=[boot()])
    sync(source)
    changed = boot(specifications={**boot()["specifications"], "crampon_attachment_types": ["strap_on"]})
    write_products(import_dir / "authorized-import.json", [changed])
    sync(source)
    product = Product.objects.get()
    assert product.specifications["crampon_attachment_types"] == ["strap_on", "semi_automatic"]
    assert product.pending_review_fields == ["crampon_attachment_types"]
    item = ReviewItem.objects.get(kind=ReviewItem.Kind.CRITICAL_CHANGE)
    assert item.proposed_value == ["strap_on"]
    # re-running does not duplicate the review item
    sync(source)
    assert ReviewItem.objects.filter(kind=ReviewItem.Kind.CRITICAL_CHANGE, status="pending").count() == 1
    with pytest.raises(ReviewError):
        decide(item.pk, approve=True, actor="", reason="x")
    decide(item.pk, approve=True, actor="alice", reason="confirmed on manufacturer page")
    product.refresh_from_db()
    assert product.specifications["crampon_attachment_types"] == ["strap_on"]
    assert product.pending_review_fields == []
    current = SpecificationEvidence.objects.get(product=product, field="crampon_attachment_types", is_current=True)
    assert current.status == SpecificationEvidence.Status.REVIEW_APPROVED
    item.refresh_from_db()
    assert (item.status, item.decided_by) == ("approved", "alice")


def test_certification_change_is_critical(structured_source, import_dir):
    crampon = {
        "external_id": "c1",
        "brand": "Test Maker",
        "name": "Ice 12",
        "category": "crampon",
        "specifications": {"binding_types": ["semi_automatic"], "declared_standards": ["EN 893"]},
    }
    source = structured_source(products=[crampon])
    sync(source)
    crampon["specifications"]["declared_standards"] = ["EN 893", "UIAA 153"]
    write_products(import_dir / "authorized-import.json", [crampon])
    sync(source)
    product = Product.objects.get()
    assert product.specifications["declared_standards"] == ["EN 893"]
    assert ReviewItem.objects.filter(kind="critical_change", field="declared_standards").exists()
    item = ReviewItem.objects.get(kind="critical_change")
    decide(item.pk, approve=False, actor="bob", reason="not on the official sheet")
    product.refresh_from_db()
    assert product.pending_review_fields == []
    assert SpecificationEvidence.objects.filter(field="declared_standards", status="rejected").count() == 1


def test_invalid_and_ambiguous_values_are_blocked_and_reviewed(structured_source):
    bad = boot(specifications={"weight_g": "2 stone", "rigidity": "bendy", "crampon_attachment_types": ["strap_on"]})
    source = structured_source(products=[bad])
    sync(source)
    product = Product.objects.get()
    assert product.specifications == {"crampon_attachment_types": ["strap_on"]}
    kinds = set(ReviewItem.objects.values_list("field", flat=True))
    assert kinds == {"weight_g", "rigidity"}
    item = ReviewItem.objects.get(field="weight_g")
    with pytest.raises(ReviewError):
        decide(item.pk, approve=True, actor="alice", reason="no value given")
    decide(item.pk, approve=True, actor="alice", reason="1250 g per manufacturer PDF", value="1250 g")
    product.refresh_from_db()
    assert product.specifications["weight_g"] == 1250


def test_new_products_wait_for_review_when_not_auto_publish(structured_source):
    source = structured_source(auto_publish=False, products=[boot()])
    sync(source)
    product = Product.objects.get()
    assert product.publication_status == Product.PublicationStatus.IN_REVIEW
    item = ReviewItem.objects.get(kind=ReviewItem.Kind.NEW_PRODUCT)
    decide(item.pk, approve=True, actor="alice", reason="checked")
    product.refresh_from_db()
    assert product.publication_status == Product.PublicationStatus.PUBLISHED


def test_similar_names_with_different_generation_are_distinct(structured_source):
    gen1 = boot(external_id="b-g1", generation="1")
    gen2 = boot(external_id="b-g2", generation="2", specifications={"weight_g": 760, "weight_basis": "per_boot"})
    source = structured_source(products=[gen1, gen2])
    run = sync(source)
    assert run.metrics["new"] == 2
    assert sorted(Product.objects.values_list("public_id", flat=True)) == [
        "test-maker-nordwand-test-1",
        "test-maker-nordwand-test-2",
    ]
    assert not ReviewItem.objects.exists()


def test_same_name_with_unknown_generation_is_never_merged(structured_source):
    source = structured_source(products=[boot(external_id="b-g2", generation="2"), boot(external_id="b-x")])
    run = sync(source)
    assert run.metrics == {**run.metrics, "new": 1, "conflicts": 1}
    item = ReviewItem.objects.get(kind=ReviewItem.Kind.AMBIGUOUS_MATCH)
    assert item.payload["candidates"] == ["test-maker-nordwand-test-2"]
    decide(item.pk, approve=True, actor="alice", reason="different model", create_new=True)
    assert Product.objects.count() == 2


def test_similar_name_is_flagged_not_merged(structured_source):
    source = structured_source(products=[boot(), boot(external_id="boot-2", name="Nordwand Tests")])
    sync(source)
    assert Product.objects.count() == 2
    flagged = Product.objects.get(name="Nordwand Tests")
    assert flagged.publication_status == Product.PublicationStatus.IN_REVIEW
    review = ReviewItem.objects.get(kind=ReviewItem.Kind.NEW_PRODUCT)
    assert "test-maker-nordwand-test" in review.payload["possible_duplicates"]


def test_weight_basis_regression(structured_source):
    per_pair = boot(external_id="pair", name="Pair Boot", specifications={"weight_g": 1500, "weight_basis": "per_pair"})
    unknown = boot(external_id="unk", name="Unknown Basis Boot", specifications={"weight_g": 700})
    source = structured_source(products=[per_pair, unknown])
    sync(source)
    assert Product.objects.get(name="Pair Boot").derived_specifications == {"weight_per_boot_g": 750.0}
    assert Product.objects.get(name="Unknown Basis Boot").derived_specifications == {}


def test_conflict_between_sources_is_recorded(structured_source):
    a = structured_source("source-a", products=[boot(mpn="NW-1")])
    sync(a)
    b = structured_source(
        "source-b",
        products=[
            boot(external_id="other-id", mpn="NW-1", specifications={"weight_g": 900, "weight_basis": "per_boot"})
        ],
    )
    run = sync(b)
    assert run.metrics["conflicts"] == 1
    product = Product.objects.get()
    assert product.specifications["weight_g"] == 800
    assert ReviewItem.objects.get(kind=ReviewItem.Kind.CONFLICT).proposed_value == 900.0


def test_missing_products_are_flagged_not_deleted(structured_source, import_dir):
    source = structured_source(products=[boot(), boot(external_id="boot-2", name="Other Boot")])
    sync(source)
    write_products(import_dir / "authorized-import.json", [boot()])
    run = sync(source)
    assert run.metrics["missing"] == 1
    other = Product.objects.get(name="Other Boot")
    assert other.publication_status == Product.PublicationStatus.PUBLISHED
    assert other.source_status == Product.SourceStatus.MISSING_AT_SOURCE
    item = ReviewItem.objects.get(kind=ReviewItem.Kind.MISSING_AT_SOURCE)
    decide(item.pk, approve=True, actor="alice", reason="discontinued")
    other.refresh_from_db()
    assert other.publication_status == Product.PublicationStatus.WITHDRAWN


def test_bad_records_do_not_abort_the_run(structured_source):
    source = structured_source(
        products=[boot(), {"external_id": "x", "brand": "B", "name": "N", "category": "kayak"}, {"external_id": "y"}]
    )
    run = sync(source)
    assert run.metrics["new"] == 1
    assert run.metrics["invalid"] == 2
    assert run.status == IngestionRun.Status.PARTIAL


def test_dry_run_writes_nothing(structured_source):
    source = structured_source(products=[boot()])
    run = run_source_sync(source, dry_run=True)
    assert run.metrics["new"] == 1
    assert Product.objects.count() == 0 and SourceRecord.objects.count() == 0


def test_disabled_or_unauthorized_sources_cannot_sync(structured_source):
    source = structured_source(products=[boot()])
    Source.objects.filter(pk=source.pk).update(enabled=False)
    source.refresh_from_db()
    with pytest.raises(SourceNotSyncable):
        run_source_sync(source)


def test_concurrent_run_is_skipped(structured_source):
    from django.core.cache import cache

    source = structured_source(products=[boot()])
    cache.add(f"ingestion-lock:{source.slug}", "1", 60)
    try:
        run = run_source_sync(source)
    finally:
        cache.delete(f"ingestion-lock:{source.slug}")
    assert run.status == IngestionRun.Status.SKIPPED
    assert Product.objects.count() == 0


def test_offers_and_price_history(structured_source, import_dir):
    from mountain_intelligence.offers.models import PriceObservation

    item = boot(
        offers=[
            {
                "retailer": "Shop",
                "currency": "EUR",
                "price": "199.90",
                "availability": "in_stock",
                "observed_at": "2026-11-20T10:00:00Z",
            }
        ]
    )
    source = structured_source(products=[item])
    sync(source)
    item["offers"][0].update(price="189.90", observed_at="2026-11-21T10:00:00Z")
    write_products(import_dir / "authorized-import.json", [item])
    sync(source)
    assert PriceObservation.objects.count() == 2
