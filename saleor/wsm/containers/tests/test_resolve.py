# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The resolver, against the payloads the TNO stage actually returned.

Recorded 2026-09-11 on the stage, zero fork code, and the four findings below
are the design constraints every test here guards. The commands:

    GET https://search.tonneauoutlaw.com/api/search/products?collection_ids=4
    GET .../api/search/products?collection_ids=4&fitment_pairs=1:18,2:2008,3:2784
    GET .../api/search/products?collection_ids=4&fitment_pairs=1:18,2:2008,3:2673

The 2019 RZR 900 returned ONLY `350-CW-xp900`; the 2019 RZR XP 1000 returned
ONLY `350-CW-xp1000`; an F-150 returned nothing; and with no vehicle all five
members came back. What the recording also proved, and what these tests exist
for:

(a) COLLECTION MEMBERSHIP IS INVISIBLE TO THE ENGINE until the product itself
    syncs, so the member list is read from Saleor and the engine is only ever
    asked what fits. `GET /api/search/products` has NO product-ids filter
    (checked, `internal/handler/search_handler.go`), so the call is still scoped
    by `collection_ids` and the staleness ceiling is named in the resolver.
(b) A member with no fitment data at all is DROPPED by the vehicle filter, not
    treated as universal. Kept here, because "at least some of them are"
    relative to fitment means the rest are not.
(c) `collection_ids` FAILS OPEN on a non-numeric id: a base64 global id returned
    46,380 products. Never sent.
(d) A product with a backdated `updatedAt` is permanently absent from the index.
    It reads as unknown, never as "does not fit".

The engine is mocked at `requests.get`, which is the real boundary: the fork
carries neither `responses` nor `respx` and a container resolve is not worth a
new dependency, so the URL the resolver builds is asserted directly and the
call count is the mock's own.
"""

import base64
import importlib
from decimal import Decimal

import pytest
import requests
from django.db import connections

from saleor.wsm.containers import pricing, resolve
from saleor.wsm.containers.models import ContainerSlot, KitConfig, KitMember

pytestmark = pytest.mark.django_db

# `0003_container_slots` is not an identifier, so the backfill this test exercises
# is reached the way Django reaches it.
backfill_migration = importlib.import_module(
    "saleor.wsm.containers.migrations.0003_container_slots"
)

ENGINE = "https://search.tonneauoutlaw.test"

RZR_900 = "1:18,2:2008,3:2784"
RZR_XP_1000 = "1:18,2:2008,3:2673"
F150 = "1:18,2:14,3:101"


def product_gid(pk) -> str:
    return base64.b64encode(f"Product:{pk}".encode()).decode()


def document(product, *, fitment_unknown=False, universal=False):
    """One engine document, in the shape the stage returned it."""
    return {
        "id": product_gid(product.pk),
        "wsm_id": "",
        "name": product.name,
        "slug": product.slug,
        "skus": [v.sku for v in product.variants.all() if v.sku],
        "collection_ids": [],
        "fitment_unknown": fitment_unknown,
        "is_universal": universal,
        "is_fitment_specific": not fitment_unknown and not universal,
    }


class FakeEngine:
    """Answers the two calls the resolver makes, and records both."""

    def __init__(self, by_vehicle, *, no_vehicle, total=None, fail=None):
        self.by_vehicle = by_vehicle
        self.no_vehicle = no_vehicle
        self.total = total
        self.fail = fail
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if self.fail is not None:
            raise self.fail
        pairs = (params or {}).get("fitment_pairs", "")
        documents = self.by_vehicle.get(pairs, []) if pairs else self.no_vehicle
        return FakeResponse(
            {
                "products": documents,
                "facets": {},
                "pagination": {
                    "total": self.total if self.total is not None else len(documents),
                    "page": 1,
                    "per_page": (params or {}).get("per_page", 20),
                    "total_pages": 1,
                },
                "search_id": "test",
            }
        )


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:  # pragma: no cover - only the ok path is used
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.payload


@pytest.fixture
def engine_url(settings):
    settings.WSM_SEARCH_ENGINE_URL = ENGINE
    return ENGINE


@pytest.fixture
def belt_saver(collection, product_list):
    """The stage container: two vehicle-specific clutch weights and a gauge.

    `product_list` prices its three members at 10.00, 20.00 and 30.00, which is
    what every money assertion in this app is already written against. The first
    two stand for `350-CW-xp900` and `350-CW-xp1000`; the third is the belt-temp
    gauge `T1-BT-FC`, which carries no fitment at all.
    """
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(
        collection=collection,
        discount_kind=pricing.PERCENT,
        discount_amount=Decimal(10),
        miss_message="Ask us about a clutch kit for your machine.",
    )
    slot = ContainerSlot.objects.create(
        kit=kit, label="Clutch weight", required=True, sort_order=0
    )
    for order, product in enumerate(product_list):
        KitMember.objects.create(
            kit=kit,
            slot=slot,
            variant=product.variants.first(),
            quantity=1,
            sort_order=order,
        )
    return kit


@pytest.fixture
def stage_engine(product_list, monkeypatch):
    """The recorded stage answers, re-keyed onto this database's product ids."""
    xp900, xp1000, gauge = product_list
    engine = FakeEngine(
        by_vehicle={
            RZR_900: [document(xp900)],
            RZR_XP_1000: [document(xp1000)],
            F150: [],
        },
        no_vehicle=[
            document(xp900),
            document(xp1000),
            document(gauge, fitment_unknown=True),
        ],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)
    return engine


# --- the requirement, both halves --------------------------------------------


def test_a_vehicle_resolves_the_slot_to_the_part_that_fits_it(
    belt_saver, stage_engine, engine_url, product_list
):
    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    assert not resolution.refused
    [slot] = resolution.slots
    skus = {candidate.variant.sku for candidate in slot.candidates}
    assert skus == {
        product_list[0].variants.first().sku,
        product_list[2].variants.first().sku,
    }
    assert {c.fitment for c in slot.candidates} == {resolve.FITS, resolve.UNIVERSAL}


def test_a_different_vehicle_resolves_the_same_slot_to_a_different_part(
    belt_saver, stage_engine, engine_url, product_list
):
    """The shopper harm the recon measured: the generic part shipping to an XP."""
    nine_hundred = resolve.resolve(belt_saver.collection, RZR_900)
    xp = resolve.resolve(belt_saver.collection, RZR_XP_1000)

    fitting = lambda r: {  # noqa: E731
        c.variant.sku for c in r.slots[0].candidates if c.fitment == resolve.FITS
    }
    assert fitting(nine_hundred) == {product_list[0].variants.first().sku}
    assert fitting(xp) == {product_list[1].variants.first().sku}
    assert fitting(nine_hundred) != fitting(xp)


def test_a_member_with_no_fitment_data_at_all_is_kept_as_universal(
    belt_saver, stage_engine, engine_url, product_list
):
    """Finding (b). The belt-temp gauge fits every machine and has no rows."""
    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    universal = [
        c for c in resolution.slots[0].candidates if c.fitment == resolve.UNIVERSAL
    ]
    assert [c.variant.sku for c in universal] == [product_list[2].variants.first().sku]


def test_a_candidate_the_engine_rejected_is_excluded_and_counted(
    belt_saver, stage_engine, engine_url
):
    """No silent drop: a slot that lost an option says how many."""
    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    assert resolution.slots[0].excluded == 1


def test_a_member_the_engine_has_never_seen_reads_unknown_and_is_kept(
    belt_saver, engine_url, product_list, monkeypatch
):
    """Finding (d). A backdated `updatedAt` is our gap, not a fitment fact."""
    xp900, _xp1000, _gauge = product_list
    engine = FakeEngine(
        by_vehicle={RZR_900: [document(xp900)]},
        # The other two products are in the collection and in no index.
        no_vehicle=[document(xp900)],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    verdicts = {c.variant.sku: c.fitment for c in resolution.slots[0].candidates}
    assert verdicts[xp900.variants.first().sku] == resolve.FITS
    assert set(verdicts.values()) == {resolve.FITS, resolve.UNKNOWN}
    assert resolution.slots[0].excluded == 0


@pytest.fixture
def no_universal_member(belt_saver, product_list):
    """The gauge leaves, so the slot holds only vehicle-specific parts.

    With it in, an F-150 still gets the gauge and the slot is NOT empty, which
    is the correct answer and the reason these three tests have to remove it.
    """
    gauge = product_list[2]
    KitMember.objects.filter(variant=gauge.variants.first()).delete()
    belt_saver.collection.products.remove(gauge)
    return belt_saver


def test_a_vehicle_nothing_fits_refuses_the_container_in_the_merchants_words(
    no_universal_member, stage_engine, engine_url
):
    belt_saver = no_universal_member

    resolution = resolve.resolve(belt_saver.collection, F150)

    assert resolution.refused
    assert resolution.refusal.code == resolve.REFUSAL_SLOT_HAS_NO_FIT
    assert resolution.refusal.slot_label == "Clutch weight"
    assert resolution.refusal.message == "Ask us about a clutch kit for your machine."


def test_a_slots_own_sentence_wins_over_the_containers(
    no_universal_member, stage_engine, engine_url
):
    belt_saver = no_universal_member
    belt_saver.slots.update(miss_message="No clutch weight for that machine yet.")

    resolution = resolve.resolve(belt_saver.collection, F150)

    assert resolution.refusal.message == "No clutch weight for that machine yet."


def test_an_optional_slot_with_nothing_that_fits_does_not_refuse_the_container(
    no_universal_member, stage_engine, engine_url
):
    belt_saver = no_universal_member
    belt_saver.slots.update(required=False)

    resolution = resolve.resolve(belt_saver.collection, F150)

    assert not resolution.refused
    assert resolution.slots[0].candidates == []
    assert resolution.selected_variant_ids() == []


# --- "sometimes there are two choices when you get down there" ----------------


def test_a_slot_that_does_not_drill_takes_every_survivor(
    belt_saver, stage_engine, engine_url
):
    """What every kit authored before slots existed does, unchanged."""
    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    slot = resolution.slots[0]
    assert not slot.drills
    assert not slot.choose
    assert len(slot.selected) == 2


def test_a_drilling_slot_with_one_survivor_auto_selects_it(
    belt_saver, stage_engine, engine_url, product_list
):
    belt_saver.slots.update(axes=["color"], partitioning_axis="color")
    # Only the fitting clutch weight survives; the gauge leaves the collection.
    belt_saver.collection.products.remove(product_list[2])
    KitMember.objects.filter(variant=product_list[2].variants.first()).delete()

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    slot = resolution.slots[0]
    assert slot.drills
    assert not slot.choose
    assert [c.variant.sku for c in slot.selected] == [
        product_list[0].variants.first().sku
    ]


def test_a_drilling_slot_with_two_survivors_asks_and_selects_nothing(
    belt_saver, stage_engine, engine_url
):
    """Dana: "sometimes there are two choices when you get down there"."""
    belt_saver.slots.update(axes=["color"], partitioning_axis="color")

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    slot = resolution.slots[0]
    assert slot.choose
    assert len(slot.candidates) == 2
    assert slot.selected == []
    assert resolution.selected_variant_ids() == []


# --- "a kit could have several series in it" ----------------------------------


def test_a_slot_pointing_at_another_collection_derives_its_candidates(
    belt_saver, engine_url, product_list, collection_list, monkeypatch
):
    """By reference, never by copy: the series stays the one place it is edited."""
    series_collection = collection_list[0]
    tuner = product_list[1]
    tuner.default_variant = tuner.variants.first()
    tuner.save(update_fields=["default_variant"])
    series_collection.products.add(tuner)
    belt_saver.members.all().delete()
    belt_saver.slots.all().delete()
    ContainerSlot.objects.create(
        kit=belt_saver,
        label="Tuner",
        required=True,
        source_collection=series_collection,
    )
    engine = FakeEngine(
        by_vehicle={RZR_XP_1000: [document(tuner)]},
        no_vehicle=[document(tuner)],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(belt_saver.collection, RZR_XP_1000)

    [slot] = resolution.slots
    [candidate] = slot.candidates
    assert candidate.member is None
    assert candidate.product_id == tuner.pk
    assert candidate.fitment == resolve.FITS
    # One call, and it names BOTH collections, so slots sharing a source share it.
    assert engine.calls[1][1]["collection_ids"] == ",".join(
        str(pk) for pk in sorted({belt_saver.collection_id, series_collection.pk})
    )


# --- default-deny -------------------------------------------------------------


def test_no_engine_configured_refuses_rather_than_showing_everything(
    belt_saver, settings, stage_engine
):
    settings.WSM_SEARCH_ENGINE_URL = ""

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    assert resolution.refused
    assert resolution.refusal.code == resolve.REFUSAL_ENGINE_UNAVAILABLE
    assert resolution.slots == []
    assert stage_engine.calls == []


def test_a_timeout_refuses_rather_than_showing_everything(
    belt_saver, engine_url, monkeypatch
):
    engine = FakeEngine(by_vehicle={}, no_vehicle=[], fail=requests.Timeout("slow"))
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    assert resolution.refusal.code == resolve.REFUSAL_ENGINE_UNAVAILABLE
    assert resolution.slots == []


def test_a_short_page_refuses_rather_than_reading_the_rest_as_no_fit(
    belt_saver, product_list, engine_url, monkeypatch
):
    """The engine pages at 20 by default. A partial answer is never resolved."""
    engine = FakeEngine(
        by_vehicle={RZR_900: [document(product_list[0])]},
        no_vehicle=[document(product_list[0])],
        total=99,
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    assert resolution.refusal.code == resolve.REFUSAL_ENGINE_UNAVAILABLE


def test_the_page_is_asked_for_at_the_size_of_the_candidate_set(
    belt_saver, stage_engine, engine_url
):
    resolve.resolve(belt_saver.collection, RZR_900)

    _url, params, _timeout = stage_engine.calls[0]
    assert params["per_page"] == 3 + resolve.ENGINE_PAGE_HEADROOM


def test_the_collection_id_reaching_the_engine_is_always_numeric(
    belt_saver, stage_engine, engine_url
):
    """Finding (c): `collection_ids` fails OPEN on anything else."""
    resolve.resolve(belt_saver.collection, RZR_900)

    for url, params, timeout in stage_engine.calls:
        assert url == ENGINE + "/api/search/products"
        assert all(part.isdigit() for part in params["collection_ids"].split(","))
        assert timeout == 4.0


def test_a_global_id_is_refused_before_a_call_is_made(stage_engine, engine_url):
    resolution = resolve.resolve(base64.b64encode(b"Collection:4").decode(), RZR_900)

    assert resolution.refusal.code == resolve.REFUSAL_BAD_COLLECTION_ID
    assert stage_engine.calls == []


def test_a_collection_that_is_not_a_container_refuses(collection, engine_url):
    resolution = resolve.resolve(collection, RZR_900)

    assert resolution.refusal.code == resolve.REFUSAL_NOT_A_CONTAINER


def test_no_vehicle_costs_no_engine_call_at_all(belt_saver, stage_engine, engine_url):
    """Before a vehicle the container is a price RANGE, not a price."""
    resolution = resolve.resolve(belt_saver.collection)

    assert stage_engine.calls == []
    assert resolution.engine_calls == 0
    assert len(resolution.slots[0].candidates) == 3
    assert all(c.fitment == resolve.UNFILTERED for c in resolution.slots[0].candidates)


# --- cost ---------------------------------------------------------------------


@pytest.mark.parametrize(("slots", "per_slot"), [(1, 1), (20, 5)])
def test_the_cost_of_a_resolve_is_flat_in_slots_and_candidates(
    collection,
    product_list,
    engine_url,
    monkeypatch,
    django_assert_num_queries,
    slots,
    per_slot,
):
    """100 members costs what 1 member costs: 3 Saleor queries, 2 engine calls.

    The refused shape is one fitment call per member, which at 20 slots of 5 is
    100 round trips on every render, repeated on the PDP, the cart and every
    revalidation. This test is the only thing standing between the two.
    """
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(collection=collection)
    for index in range(slots):
        slot = ContainerSlot.objects.create(
            kit=kit, label=f"Slot {index}", required=False, sort_order=index
        )
        for order in range(per_slot):
            product = product_list[order % len(product_list)]
            variant = product.variants.create(
                sku=f"slot-{index}-cand-{order}", name=f"{index}-{order}"
            )
            KitMember.objects.create(
                kit=kit, slot=slot, variant=variant, quantity=1, sort_order=order
            )

    engine = FakeEngine(
        by_vehicle={RZR_900: [document(p) for p in product_list]},
        no_vehicle=[document(p) for p in product_list],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    with django_assert_num_queries(3):
        resolution = resolve.resolve(kit.collection, RZR_900)

    assert not resolution.refused
    assert len(resolution.slots) == slots
    assert sum(len(s.candidates) for s in resolution.slots) == slots * per_slot
    assert len(engine.calls) == 2
    assert resolution.engine_calls == 2


# --- the backfill -------------------------------------------------------------


class _HistoricalApps:
    """The models the data migration asks for, which are field-identical here."""

    _models = {
        "KitConfig": KitConfig,
        "KitMember": KitMember,
        "ContainerSlot": ContainerSlot,
    }

    def get_model(self, _app_label, name):
        return self._models[name]


class _SchemaEditor:
    connection = connections["default"]


def test_the_backfill_puts_every_member_of_a_kit_in_one_fixed_slot(
    collection, product_list
):
    """Run the migration's own function over rows written without slots."""
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(collection=collection)
    for order, product in enumerate(product_list):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    assert not kit.slots.exists()

    backfill_migration.put_every_member_in_one_fixed_slot(
        _HistoricalApps(), _SchemaEditor()
    )

    [slot] = list(kit.slots.all())
    assert slot.label == backfill_migration.BACKFILL_SLOT_LABEL
    # No partitioning axis IS the data that says "take everything that survives",
    # which is why no existing kit changes price.
    assert slot.partitioning_axis == ""
    assert slot.candidates.count() == 3
    assert not KitMember.objects.filter(kit=kit, slot__isnull=True).exists()


def test_the_backfill_reverse_unpoints_members_before_deleting_slots(
    collection, product_list
):
    """A reverse that cascaded would delete the members it was protecting."""
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(collection=collection)
    for order, product in enumerate(product_list):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    backfill_migration.put_every_member_in_one_fixed_slot(
        _HistoricalApps(), _SchemaEditor()
    )

    backfill_migration.take_every_member_out_of_its_slot(
        _HistoricalApps(), _SchemaEditor()
    )

    assert kit.members.count() == 3
    assert ContainerSlot.objects.count() == 0


def test_a_kit_with_no_members_gets_no_slot(collection):
    """A role nothing can fill is a row nobody would ever read."""
    kit = KitConfig.objects.create(collection=collection)

    backfill_migration.put_every_member_in_one_fixed_slot(
        _HistoricalApps(), _SchemaEditor()
    )

    assert not kit.slots.exists()


def test_a_derived_product_with_no_sku_to_sell_is_counted_not_vanished(
    belt_saver, engine_url, product_list, collection_list, monkeypatch
):
    """A product with no default variant has nothing to put in a cart."""
    series_collection = collection_list[0]
    series_collection.products.add(product_list[1])
    belt_saver.members.all().delete()
    belt_saver.slots.all().delete()
    ContainerSlot.objects.create(
        kit=belt_saver,
        label="Tuner",
        required=False,
        source_collection=series_collection,
    )
    engine = FakeEngine(by_vehicle={RZR_900: []}, no_vehicle=[])
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(belt_saver.collection, RZR_900)

    [slot] = resolution.slots
    assert slot.candidates == []
    assert slot.excluded == 1
