# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""`wsm.price_floor`: the lowest price a shopper can actually pay for a product.

Dana ruled on 2026-09-09 that a configurable product may keep a $0 base price
for the bake-off, which took the base price out of service as a number anyone
can be quoted. Google Merchant Center suspends a feed on a zero price, a PLP
tile with no number is not a tile anyone clicks, and the search engine has
nothing to sort or filter on. So the floor is computed once, on the merchant
save that moves it, and stamped on the product for all three to read.

Money is asserted as STRINGS throughout. A Decimal or float assertion here would
pass on a stamp that serialises 493.2399999999998, which is what the consumers
would then quote.
"""

import json
import logging
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from saleor.product.models import ProductVariantChannelListing
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    PRICE_FLOOR_METAFIELD,
    Fee,
    OptionSet,
    OptionValue,
)


def floor_of(product):
    """The stamp as a consumer parses it, or None when there is no stamp.

    Read back through `json.loads` on purpose: the value is a JSON STRING,
    because GraphQL types a metadata value as String and a dict would reach the
    feed as a Python repr. A test that read `product.metadata[key]` as a dict
    would pass on a stamp no consumer can parse.
    """
    product.refresh_from_db()
    raw = product.metadata.get(PRICE_FLOOR_METAFIELD)
    return None if raw is None else json.loads(raw)


@pytest.fixture
def priced(product):
    """The stock product fixture, repriced to a round 100.00 in one channel.

    Written through `update()` so no stamp is left behind by the setup itself:
    every test below starts from a product with a base price and no floor.
    """
    ProductVariantChannelListing.objects.filter(variant__product=product).update(
        price_amount=Decimal("100.00")
    )
    return product


def _required_finish(product, cheapest="-10", dearest="25"):
    option_set = OptionSet.objects.create(
        product=product, name="Finish", required=True
    )
    OptionValue.objects.create(
        option_set=option_set, name="Raw", price_delta=Decimal(cheapest)
    )
    if dearest is not None:
        OptionValue.objects.create(
            option_set=option_set, name="Anodised", price_delta=Decimal(dearest)
        )
    return option_set


def test_a_required_question_stamps_its_cheapest_answer(priced, channel_USD):
    """The floor is the base plus the cheapest legal answer, credit included.

    A required question has no "skip", so its cheapest value is money the
    shopper always pays or always saves. 100.00 with a -10.00 credit on the
    table is a product nobody can buy for 100.00.
    """
    _required_finish(priced)

    assert floor_of(priced) == {
        channel_USD.slug: {"amount": "90.00", "currency": "USD"}
    }


def test_a_question_the_shopper_can_skip_leaves_the_floor_at_the_base(
    priced, channel_USD
):
    """An optional surcharge is not a price anyone has to pay."""
    option_set = OptionSet.objects.create(
        product=priced, name="Engraving", required=False
    )
    OptionValue.objects.create(
        option_set=option_set, name="Engraved", price_delta=Decimal("25")
    )

    assert floor_of(priced)[channel_USD.slug]["amount"] == "100.00"


def test_a_required_charge_is_part_of_the_lowest_price_anyone_pays(
    priced, channel_USD
):
    """A crating charge that is always on is part of the price, not an extra."""
    Fee.objects.create(
        product=priced, label="Crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    assert floor_of(priced)[channel_USD.slug]["amount"] == "249.00"


def test_a_declinable_charge_is_not_part_of_the_floor(priced):
    """A charge the shopper can refuse is an optional question in fee clothing.

    And with nothing else on the product the floor IS the base price, which the
    listing already carries, so there is no stamp at all rather than a second
    copy of a number to go stale.
    """
    Fee.objects.create(
        product=priced,
        label="Crating",
        basis=pricing.FIXED,
        amount=Decimal("149"),
        required=False,
        decline_label="No crate, I will collect",
    )

    assert floor_of(priced) is None


def test_a_percentage_charge_computes_on_the_cheapest_subtotal(priced, channel_USD):
    """10% of the 90.00 the cheapest answer produces, never 10% of the base."""
    _required_finish(priced, dearest=None)
    Fee.objects.create(
        product=priced, label="Handling", basis=pricing.PERCENT, amount=Decimal("10")
    )

    assert floor_of(priced)[channel_USD.slug]["amount"] == "99.00"


def test_each_channel_gets_its_own_floor_in_its_own_currency(
    priced, channel_USD, channel_PLN
):
    """A base price is per channel, so a floor is too.

    The option delta is one number on one row and is not itself per channel,
    which is the data model as it stands: it lands in each channel's own
    currency. Named here so the next reader knows it was measured, not missed.
    """
    ProductVariantChannelListing.objects.create(
        variant=priced.variants.first(),
        channel=channel_PLN,
        currency=channel_PLN.currency_code,
        price_amount=Decimal("400.00"),
        discounted_price_amount=Decimal("400.00"),
    )

    _required_finish(priced)

    assert floor_of(priced) == {
        channel_USD.slug: {"amount": "90.00", "currency": "USD"},
        channel_PLN.slug: {"amount": "390.00", "currency": "PLN"},
    }


def test_editing_one_answer_restamps_the_floor(priced, channel_USD):
    """The save path that exists for the floor alone.

    An option value cannot change whether a product is configurable, so the
    marker's hooks never watched it; the cheapest answer to a required question
    IS the floor, so a merchant retyping one price has to leave a current stamp.
    """
    _required_finish(priced, dearest=None)
    assert floor_of(priced)[channel_USD.slug]["amount"] == "90.00"

    value = OptionValue.objects.get(option_set__product=priced, name="Raw")
    value.price_delta = Decimal("-30")
    value.save()

    assert floor_of(priced)[channel_USD.slug]["amount"] == "70.00"


def test_deleting_an_answer_restamps_the_floor(priced, channel_USD):
    """Removing the cheapest choice raises the floor to the next one."""
    _required_finish(priced)
    assert floor_of(priced)[channel_USD.slug]["amount"] == "90.00"

    OptionValue.objects.get(option_set__product=priced, name="Raw").delete()

    assert floor_of(priced)[channel_USD.slug]["amount"] == "125.00"


def test_deleting_the_last_question_takes_the_key_off(priced):
    """Absent, never an empty object: absent is what a reader reads as no floor."""
    option_set = _required_finish(priced)
    assert floor_of(priced) is not None

    option_set.delete()

    assert floor_of(priced) is None
    priced.refresh_from_db()
    assert PRICE_FLOOR_METAFIELD not in priced.metadata


def test_a_floor_below_zero_stamps_zero_and_says_so(priced, channel_USD, caplog):
    """A $0 base with a credit on it prices below nothing, which nothing can quote.

    The admin refuses to save this and `pricing.delta_for` refuses to charge it,
    so rows that reach here were written around both. Stamped at zero rather
    than negative, because a negative "from" price is a feed rejection and a
    broken tile, and logged with the product id so it can be found.
    """
    ProductVariantChannelListing.objects.filter(variant__product=priced).update(
        price_amount=Decimal("0")
    )
    caplog.set_level(logging.WARNING, logger="saleor.wsm.compose.models")

    _required_finish(priced, cheapest="-5", dearest=None)

    assert floor_of(priced)[channel_USD.slug]["amount"] == "0.00"
    assert f"product {priced.pk}" in caplog.text
    assert "below zero" in caplog.text


def test_a_product_with_no_priced_listing_stamps_nothing(product):
    """A half-built catalog gets no stamp rather than a stamp saying 0.00."""
    ProductVariantChannelListing.objects.filter(variant__product=product).delete()

    _required_finish(product)

    assert floor_of(product) is None


def test_the_command_restamps_a_base_price_the_signals_never_saw(
    priced, channel_USD
):
    """The known gap, and its answer.

    A base price lives on a core table Saleor writes through `bulk_update` on
    the discount path, which fires no signal, so the stamp goes stale on a price
    change and the command is what makes it current again.
    """
    _required_finish(priced, dearest=None)
    assert floor_of(priced)[channel_USD.slug]["amount"] == "90.00"

    ProductVariantChannelListing.objects.filter(variant__product=priced).update(
        price_amount=Decimal("250.00")
    )
    assert floor_of(priced)[channel_USD.slug]["amount"] == "90.00"

    call_command("wsm_stamp_price_floor")

    assert floor_of(priced)[channel_USD.slug]["amount"] == "240.00"


def test_a_second_run_of_the_command_writes_nothing(priced):
    """Idempotent, proved by the queries: reads only, not one UPDATE."""
    _required_finish(priced, dearest=None)
    call_command("wsm_stamp_price_floor")

    with CaptureQueriesContext(connection) as captured:
        call_command("wsm_stamp_price_floor")

    writes = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
    ]
    assert writes == []
    # Seven reads for one product and no write: the two that pick the products
    # to look at, then the product row, its sets, their values, its fees and one
    # grouped query for every channel's base price. Pinned because a number that
    # moves here is a cost regression on the merchant save path.
    assert len(captured.captured_queries) == 7, [
        query["sql"][:70] for query in captured.captured_queries
    ]


def test_one_answer_saved_costs_seven_queries_on_top_of_the_write(
    priced, django_assert_num_queries
):
    """The whole cost of the stamp, on the path a merchant drives by hand.

    Eight: the value's own UPDATE, then the product id one join away, the
    product row, its sets, their values, its fees, one grouped query for every
    channel's base price, and one UPDATE of `metadata`. Zero of them are on any
    shopper path, which is the entire point of stamping.
    """
    option_set = OptionSet.objects.create(
        product=priced, name="Finish", required=True
    )
    value = OptionValue.objects.create(
        option_set=option_set, name="Raw", price_delta=Decimal("-10")
    )

    value.price_delta = Decimal("-12")
    with django_assert_num_queries(8):
        value.save()
