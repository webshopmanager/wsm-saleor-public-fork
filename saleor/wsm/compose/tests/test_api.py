# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two endpoints, exercised the way wsm-storefront calls them.

Every expected number is written out from the requirements fixture (Stage 2 Kit
at 3998.99 with the three credits), never derived from the code under test. The
assertions are on what the CHECKOUT ends up holding, not on the response alone:
a response that says 3494.00 over a line stored at 3998.99 is the failure this
unit exists to prevent.
"""

import base64
import json
from decimal import Decimal

import pytest

from saleor.wsm.compose.lines import (
    META_CID,
    META_FEE,
    META_OPTIONS,
    META_PARENT,
    META_SKU,
    PRICE_OVERRIDE_REASON,
)
from saleor.wsm.compose.models import Fee, OptionSet, OptionValue
from saleor.wsm.dealer.no_stacking import LINE_METADATA_KEY as DEALER_KEY
from saleor.wsm.tests import COMPOSE_HEADERS

OPTION_SETS_URL = "/wsm/compose/api/storefront/products/saleor/{}/option-sets"
CONFIGURED_LINE_URL = "/wsm/compose/api/checkout/configured-line"

BASE_PRICE = Decimal("3998.99")
CONFIGURED_UNIT = "3494.00"  # 3998.99 - 29.99 - 30.00 - 445.00
FEE_AMOUNT = "149.00"

HEADERS = COMPOSE_HEADERS


def gid(type_name, pk):
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


@pytest.fixture
def stage_2_kit(variant, channel_USD):
    """The requirements fixture's L600084, on Saleor's stock variant fixture."""
    variant.sku = "L600084"
    # Made to order, like the kits this models: the quantity path under test is
    # the price_override surviving a quantity change, not stock reservation.
    variant.track_inventory = False
    variant.save(update_fields=["sku", "track_inventory"])
    listing = variant.channel_listings.get(channel=channel_USD)
    listing.price_amount = BASE_PRICE
    listing.discounted_price_amount = BASE_PRICE
    listing.save(update_fields=["price_amount", "discounted_price_amount"])
    return variant


@pytest.fixture
def omit_parts(stage_2_kit):
    option_set = OptionSet.objects.create(
        product=stage_2_kit.product,
        name="Omit parts",
        label="Omit parts",
        prompt_type="choice_many",
        required=True,
        note="Credits for parts the customer already owns.",
    )
    values = [
        OptionValue.objects.create(
            option_set=option_set,
            name=name,
            sku_fragment=fragment,
            price_delta=Decimal(delta),
            sort_order=i,
        )
        for i, (name, fragment, delta) in enumerate(
            [
                ("Omit fuel filter", "NOFF", "-29.99"),
                ("Omit fuel lines", "NOFL", "-30.00"),
                ("Omit pump assembly", "NOPA", "-445.00"),
            ]
        )
    ]
    return option_set, values


@pytest.fixture
def crating_fee(stage_2_kit):
    return Fee.objects.create(
        product=stage_2_kit.product,
        label="Freight crating",
        sku="CRATE-01",
        basis="fixed",
        amount=Decimal(FEE_AMOUNT),
        apply_to="unit",
        required=True,
    )


def post_line(
    client, checkout, variant, *, quantity=1, selections, accepted=(), customer=None
):
    body = {
        "checkoutId": gid("Checkout", checkout.token),
        "channel": checkout.channel.slug,
        "productId": gid("Product", variant.product_id),
        "variantId": gid("ProductVariant", variant.pk),
        "quantity": quantity,
        "selections": selections,
        "acceptedFeeIds": list(accepted),
    }
    if customer is not None:
        body["customerId"] = gid("User", customer.pk)
    return client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(body),
        content_type="application/json",
        **HEADERS,
    )


# --- (a) the PDP read ------------------------------------------------------


def test_option_sets_returns_the_contract_shape(
    client, stage_2_kit, omit_parts, crating_fee
):
    option_set, values = omit_parts

    response = client.get(
        OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == [
        {
            "id": option_set.pk,
            "name": "Omit parts",
            "label": "Omit parts",
            "prompt_type": "choice_many",
            "required": True,
            "note": "Credits for parts the customer already owns.",
            "deselect_prompt": "",
            "values": [
                {
                    "id": values[0].pk,
                    "name": "Omit fuel filter",
                    "sku_fragment": "NOFF",
                    "price_delta": "-29.99",
                    "image_url": "",
                    "help_text": "",
                    "is_default": False,
                },
                {
                    "id": values[1].pk,
                    "name": "Omit fuel lines",
                    "sku_fragment": "NOFL",
                    "price_delta": "-30.00",
                    "image_url": "",
                    "help_text": "",
                    "is_default": False,
                },
                {
                    "id": values[2].pk,
                    "name": "Omit pump assembly",
                    "sku_fragment": "NOPA",
                    "price_delta": "-445.00",
                    "image_url": "",
                    "help_text": "",
                    "is_default": False,
                },
            ],
        }
    ]
    assert body["fees"] == [
        {
            "id": crating_fee.pk,
            "label": "Freight crating",
            "sku": "CRATE-01",
            "basis": "fixed",
            "amount": "149.00",
            "apply_to": "unit",
            "required": True,
            "decline_label": "",
        }
    ]


def test_option_sets_accepts_a_gid_that_lost_its_padding(
    client, stage_2_kit, omit_parts
):
    raw = gid("Product", stage_2_kit.product_id).rstrip("=")

    assert client.get(OPTION_SETS_URL.format(raw)).status_code == 200


def test_option_sets_unknown_product_is_404(client, db):
    response = client.get(OPTION_SETS_URL.format(gid("Product", 999999)))

    assert response.status_code == 404
    assert response.json() == {"violations": ["unknown product"]}


def test_option_sets_stays_inside_its_query_budget(
    client, stage_2_kit, omit_parts, crating_fee, django_assert_num_queries
):
    # Design section 5, restated from the measurement: one read to prove the
    # product is published somewhere (the anonymous endpoint says nothing about
    # an unreleased product), one for the sets and their values (a prefetch is
    # two statements), one for the fees, and since design section 11 one for the
    # product's Prop 65 row. Flat: 5 at one set and two values, 5 at five sets
    # and fifty. Anything more is an N+1 creeping in.
    with django_assert_num_queries(5):
        client.get(OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id)))


# --- (b) and (c) the configured add ----------------------------------------


def test_configured_line_prices_server_side_and_writes_one_line(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    _, values = omit_parts

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": omit_parts[0].pk, "value_ids": [v.pk for v in values]}],
        accepted=[crating_fee.pk],
    )

    assert response.status_code == 200
    assert response.json() == {
        "checkoutId": gid("Checkout", checkout.token),
        "unitPrice": CONFIGURED_UNIT,
        "compositeSku": "L600084-NOFF-NOFL-NOPA",
        "feeTotal": FEE_AMOUNT,
        # Design section 11: the disclosures the cart has to draw, empty here
        # because this product carries no compliance row.
        "warnings": [],
    }

    lines = list(checkout.lines.all())
    assert len(lines) == 2
    product_line = next(li for li in lines if li.variant_id == stage_2_kit.pk)
    fee_line = next(li for li in lines if li.variant_id != stage_2_kit.pk)

    # The money is on the line, not only in the response.
    assert product_line.price_override == Decimal(CONFIGURED_UNIT)
    assert product_line.price_override_reason == PRICE_OVERRIDE_REASON
    assert fee_line.price_override == Decimal(FEE_AMOUNT)

    assert product_line.metadata[META_SKU] == "L600084-NOFF-NOFL-NOPA"
    assert json.loads(product_line.metadata[META_OPTIONS])["unit_cents"] == 349400
    cid = product_line.metadata[META_CID]
    assert fee_line.metadata[META_CID] == cid
    assert json.loads(fee_line.metadata[META_FEE]) == {
        "label": "Freight crating",
        "apply_to": "unit",
    }
    # The fee names its parent by CID, never by pk: that is the only key the
    # storefront pairs on, and a pk here orphans the fee in the cart.
    assert fee_line.metadata[META_PARENT] == cid

    # The fee's own product is never something a shopper can browse to.
    fee_listing = fee_line.variant.product.channel_listings.get(
        channel=checkout.channel
    )
    assert fee_listing.visible_in_listings is False


def test_configured_line_survives_a_stock_quantity_update(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    """The whole reason the price is a price_override and not a plugin hook."""
    _, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        quantity=2,
        selections=[{"set_id": omit_parts[0].pk, "value_ids": [v.pk for v in values]}],
        accepted=[crating_fee.pk],
    )
    product_line = checkout.lines.get(variant_id=stage_2_kit.pk)

    response = client.post(
        "/graphql/",
        data=json.dumps(
            {
                "query": """
                mutation($id: ID!, $line: ID!) {
                  checkoutLinesUpdate(id: $id, lines: [{lineId: $line, quantity: 3}]) {
                    errors { field message }
                    checkout { lines { quantity unitPrice { gross { amount } } } }
                  }
                }""",
                "variables": {
                    "id": gid("Checkout", checkout.token),
                    "line": gid("CheckoutLine", product_line.pk),
                },
            }
        ),
        content_type="application/json",
    )

    payload = response.json()["data"]["checkoutLinesUpdate"]
    assert payload["errors"] == []
    product_line.refresh_from_db()
    assert product_line.quantity == 3
    assert product_line.price_override == Decimal(CONFIGURED_UNIT)
    assert {
        (line["quantity"], line["unitPrice"]["gross"]["amount"])
        for line in payload["checkout"]["lines"]
    } == {(3, 3494.0), (2, 149.0)}


def test_configured_line_stays_inside_its_query_budget(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    """The steady-state cost of a configured add.

    Measured on the box against the live catalog: 36 queries warm, where a stock
    `checkoutLinesAdd` of the same variant costs 56. Six of the 36 are Compose's
    own reads; the rest is Saleor's recalculation, which is the whole point of
    using its write path instead of inventing one. The FIRST add of a fee also
    creates that fee's hidden variant, so the budget is measured on the second.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _, values = omit_parts
    selections = [{"set_id": omit_parts[0].pk, "value_ids": [v.pk for v in values]}]
    post_line(
        client, checkout, stage_2_kit, selections=selections, accepted=[crating_fee.pk]
    )

    with CaptureQueriesContext(connection) as captured:
        post_line(
            client,
            checkout,
            stage_2_kit,
            selections=selections,
            accepted=[crating_fee.pk],
        )

    assert len(captured.captured_queries) <= 45, (
        f"configured add cost {len(captured.captured_queries)} queries"
    )


# --- (e) and (f) the refusals ----------------------------------------------


def test_a_configuration_priced_below_zero_is_refused(
    client, checkout, stage_2_kit, omit_parts
):
    option_set, _ = omit_parts
    over_credit = OptionValue.objects.create(
        option_set=option_set,
        name="Full trade-in",
        sku_fragment="TRADE",
        price_delta=Decimal("-7000.00"),
    )

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [over_credit.pk]}],
    )

    assert response.status_code == 422
    # The shopper reads this. Currency units, never cents (defect 5).
    assert "-3,001.01" in response.json()["violations"][0]
    assert "not a valid price" in response.json()["violations"][0]
    assert checkout.lines.count() == 0


def test_a_missing_required_set_is_refused(client, checkout, stage_2_kit, omit_parts):
    response = post_line(client, checkout, stage_2_kit, selections=[])

    assert response.status_code == 422
    assert response.json() == {
        "violations": ["required option 'Omit parts' not selected"]
    }
    assert checkout.lines.count() == 0


def test_an_unknown_value_is_refused(client, checkout, stage_2_kit, omit_parts):
    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": omit_parts[0].pk, "value_ids": [999999]}],
    )

    assert response.status_code == 422
    assert checkout.lines.count() == 0


def test_an_unknown_checkout_is_404(client, checkout, stage_2_kit, omit_parts):
    response = client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", "11111111-1111-1111-1111-111111111111"),
                "channel": checkout.channel.slug,
                "productId": gid("Product", stage_2_kit.product_id),
                "variantId": gid("ProductVariant", stage_2_kit.pk),
                "quantity": 1,
                "selections": [],
            }
        ),
        content_type="application/json",
        **HEADERS,
    )

    assert response.status_code == 404
    assert response.json() == {"violations": ["unknown checkout"]}


def test_an_unknown_product_is_404(client, checkout, stage_2_kit):
    response = client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", checkout.token),
                "channel": checkout.channel.slug,
                "productId": gid("Product", 999999),
                "variantId": gid("ProductVariant", stage_2_kit.pk),
                "quantity": 1,
                "selections": [],
            }
        ),
        content_type="application/json",
        **HEADERS,
    )

    assert response.status_code == 404


def test_naming_a_required_fee_as_accepted_is_not_an_error(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    """The storefront posts back every fee the shopper saw, required included.

    The pricing engine refuses that id (accepting a required fee is meaningless
    there), so the view drops it before pricing. The fee is charged either way.
    """
    _, values = omit_parts

    without = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": omit_parts[0].pk, "value_ids": [v.pk for v in values]}],
    )

    assert without.status_code == 200
    assert without.json()["feeTotal"] == FEE_AMOUNT


# --- (d) the dealer on a configured line -----------------------------------


@pytest.fixture
def dealer_credit(customer_user, omit_parts):
    """A dealer whose credit on the pump assembly is deeper than retail's."""
    from saleor.wsm.compose.models import DealerTierOptionPrice
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup

    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    DealerTierOptionPrice.objects.create(
        option_value=omit_parts[1][2],
        tier_group="dealer-1",
        price_delta=Decimal("-545.00"),
    )
    return customer_user


def test_a_dealer_pays_the_tier_delta_on_the_configured_line(
    client, checkout, stage_2_kit, omit_parts, dealer_credit
):
    """B3 on a configured line: the tier row prices it, not the retail delta."""
    option_set, values = omit_parts

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        customer=dealer_credit,
    )

    assert response.status_code == 200
    # 3998.99 - 29.99 - 30.00 - 545.00: the dealer's credit stands in place of
    # retail's -445.00 and nothing else about the line moves.
    assert response.json()["unitPrice"] == "3394.00"
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == Decimal("3394.00")
    snapshot = json.loads(line.metadata[META_OPTIONS])
    assert snapshot["tier_group"] == "dealer-1"
    assert snapshot["tier_applied"] is True


def test_a_retail_shopper_is_unmoved_by_a_tier_row(
    client, checkout, stage_2_kit, omit_parts, dealer_credit
):
    """The acceptance number, with the dealer row sitting right there: 3494.00."""
    option_set, values = omit_parts

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
    )

    assert response.json()["unitPrice"] == CONFIGURED_UNIT
    assert checkout.lines.get(variant_id=stage_2_kit.pk).price_override == Decimal(
        CONFIGURED_UNIT
    )


def test_option_sets_never_quotes_a_dealer_delta_to_the_public(
    client, stage_2_kit, omit_parts, dealer_credit
):
    """The one ungated read answers retail, whoever it is asked about.

    `dealer_credit` is a real dealer with a real -545.00 row on the third value.
    Passing their global id, which is a base64 of a sequential integer and needs
    no authentication of any kind, must not turn the public product page into a
    dealer price book.
    """
    url = OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id))
    retail_deltas = ["-29.99", "-30.00", "-445.00"]

    retail = client.get(url).json()["data"][0]["values"]
    asked = client.get(url, {"customerId": gid("User", dealer_credit.pk)}).json()[
        "data"
    ][0]["values"]

    assert [v["price_delta"] for v in retail] == retail_deltas
    assert [v["price_delta"] for v in asked] == retail_deltas


# --- finding 8: a posted quantity that is not a number is a 422, not a 500 ---


def test_a_quantity_that_is_not_a_number_is_refused(client, checkout, stage_2_kit):
    response = client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", checkout.token),
                "channel": checkout.channel.slug,
                "productId": gid("Product", stage_2_kit.product_id),
                "variantId": gid("ProductVariant", stage_2_kit.pk),
                "quantity": "x",
                "selections": [],
            }
        ),
        content_type="application/json",
        **HEADERS,
    )

    assert response.status_code == 422
    assert response.json() == {"violations": ["quantity must be a whole number"]}


def test_a_variant_not_available_for_purchase_is_refused(
    client, checkout, stage_2_kit, omit_parts
):
    """The stock mutation refuses this; so does the fork's own write path."""
    stage_2_kit.product.channel_listings.filter(channel=checkout.channel).update(
        available_for_purchase_at=None
    )
    option_set, values = omit_parts

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
    )

    assert response.status_code == 422
    assert checkout.lines.count() == 0


def test_a_tax_exempt_dealers_configured_line_lands_in_a_cart_that_owes_no_tax(
    client, checkout, stage_2_kit, omit_parts, dealer_credit
):
    """A cart of nothing but configured products is still a dealer's cart.

    The exemption is a fact about the BUYER, not about the shape of what they
    put in the cart, so it lands on the route that priced it. Waiting for the
    shopper to also add a plain SKU through the dealer endpoint would tax a
    whole Fuel Lab order, where every line is a configuration.

    `Checkout.tax_exemption` is Saleor's own field and stock Saleor does the
    rest of the work: `saleor/wsm/dealer/tax.py` explains what may write it.
    """
    from saleor.wsm.dealer.models import DealerCustomer

    option_set, values = omit_parts
    DealerCustomer.objects.filter(user=dealer_credit).update(tax_exempt=True)

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        customer=dealer_credit,
    )

    assert response.status_code == 200, response.content
    checkout.refresh_from_db()
    assert checkout.tax_exemption is True


def test_a_configured_line_starts_from_the_sale_price(
    client, checkout, stage_2_kit, omit_parts
):
    """The same defect as the kit member, on the configured line's own base.

    A merchant who puts a configurable product on a catalogue promotion has
    dropped its price: the option deltas come off what it sells for today, not
    off the list price the storefront is already striking through.
    """
    option_set, values = omit_parts
    stage_2_kit.channel_listings.filter(channel=checkout.channel).update(
        discounted_price_amount=Decimal("3500.00")
    )

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
    )

    assert response.status_code == 200, response.content
    # 3500.00 - 29.99 - 30.00 - 445.00, where list would have given 3494.00.
    assert response.json()["unitPrice"] == "2995.01"
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == Decimal("2995.01")


# --- (e) the dealer's base price under a configured line --------------------

# What Jobbers pay for the Stage 2 Kit itself: a tier row well under the
# 3998.99 the listing quotes everyone else.
JOBBER_BASE = Decimal("3200.00")


@pytest.fixture
def buckle(stage_2_kit):
    """One priced add-on: the Build-A-Belt shape, a positive delta on a base."""
    option_set = OptionSet.objects.create(
        product=stage_2_kit.product,
        name="Buckle",
        label="Buckle",
        prompt_type="choice_one",
        required=True,
    )
    value = OptionValue.objects.create(
        option_set=option_set,
        name="Plastic Push with Snap Hooks",
        sku_fragment="PPSH",
        price_delta=Decimal("8.00"),
        sort_order=0,
    )
    return option_set, value


@pytest.fixture
def jobber(customer_user, stage_2_kit, channel_USD):
    """A dealer priced on the VARIANT and on no option value at all.

    Deliberately the opposite of `dealer_credit`: that fixture proves the tier
    row on a value is honoured, and every number it asserts is still correct
    when the base underneath it is wrong, which is how the base stayed retail.
    """
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="jobbers", name="Jobbers")
    DealerCustomer.objects.create(user=customer_user, group=group)
    TierPrice.objects.create(
        variant=stage_2_kit, group=group, min_quantity=1, amount=JOBBER_BASE
    )
    return customer_user


def test_a_dealers_configured_line_starts_from_his_own_price(
    client, checkout, stage_2_kit, buckle, jobber
):
    """The Build-A-Belt defect, in this fixture's numbers.

    Measured on the demo stage 2026-09-09: a Jobber was quoted 31.36 on the PDP
    (his 23.36 plus an 8.00 buckle) and charged 36.95 in the cart (retail 28.95
    plus the same 8.00), because the configured line was built on the channel
    listing and the buyer's own price was never asked for. 17.8% over.
    """
    option_set, value = buckle

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
        customer=jobber,
    )

    assert response.status_code == 200
    # 3200.00 + 8.00, never 3998.99 + 8.00.
    assert response.json()["unitPrice"] == "3208.00"
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == Decimal("3208.00")
    snapshot = json.loads(line.metadata[META_OPTIONS])
    assert snapshot["base_unit_cents"] == 320000
    # The tier decided this price, so the line has to SAY it is a dealer line:
    # without the stamp a voucher or a catalogue promotion comes off a price
    # that is already the dealer's.
    assert snapshot["tier_applied"] is True
    assert DEALER_KEY in line.private_metadata


def test_a_retail_shopper_is_unmoved_by_a_tier_price_on_the_variant(
    client, checkout, stage_2_kit, buckle, jobber
):
    """The dealer's row is sitting right there and retail still pays retail."""
    option_set, value = buckle

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
    )

    assert response.json()["unitPrice"] == "4006.99"
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == Decimal("4006.99")
    assert DEALER_KEY not in line.private_metadata


def test_a_dealer_never_pays_his_tier_when_the_shop_is_selling_it_for_less(
    client, checkout, stage_2_kit, buckle, jobber, channel_USD
):
    """Better-of, the same rule a kit member and an option delta already follow.

    A merchant who puts a product on sale below a dealer's negotiated price has
    made the sale price the better one, and quoting the dealer the higher of the
    two is the complaint that arrives by phone.
    """
    option_set, value = buckle
    stage_2_kit.channel_listings.filter(channel=channel_USD).update(
        discounted_price_amount=Decimal("3000.00")
    )

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
        customer=jobber,
    )

    assert response.json()["unitPrice"] == "3008.00"
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    snapshot = json.loads(line.metadata[META_OPTIONS])
    # The tier lost, so nothing may claim it applied: a line stamped as
    # dealer-priced when it is not is how a promotion gets suppressed on a
    # retail price.
    assert snapshot["tier_applied"] is False
    assert DEALER_KEY not in line.private_metadata


def test_a_break_this_quantity_does_not_reach_leaves_the_base_at_retail(
    client, checkout, stage_2_kit, buckle, jobber
):
    """The base follows the same ladder rule as a plain dealer line: by quantity."""
    from saleor.wsm.dealer.models import TierPrice

    TierPrice.objects.filter(variant=stage_2_kit).update(min_quantity=5)
    option_set, value = buckle

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
        customer=jobber,
    )

    assert response.json()["unitPrice"] == "4006.99"


# --- (d) the stamp lands on the lines THIS request created -----------------


def test_a_second_configured_add_leaves_the_first_adds_lines_alone(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    """Two configurations of one product are two priced items, not one restamped twice.

    The stamp was keyed by VARIANT, so the second add rewrote the first item
    line's options snapshot (MP3 then repriced that line to the SECOND
    configuration, undercharging by the difference) and gave the first fee line
    the second request's cid (both fee lines then shared a parent AND a variant,
    collapsed to one entry in `reprice.fee_lines_by_parent`, and one required
    149.00 charge fell out of the cart as an orphan). No key and no forgery: a
    shopper buying two configurations of one part.
    """
    option_set, values = omit_parts

    first = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [values[0].pk]}],
    )
    assert first.status_code == 200, first.content
    after_first = {
        line.pk: (dict(line.private_metadata), line.price_override)
        for line in checkout.lines.all()
    }
    assert len(after_first) == 2

    second = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
    )
    assert second.status_code == 200, second.content

    after_second = {
        line.pk: (dict(line.private_metadata), line.price_override)
        for line in checkout.lines.all()
    }
    assert len(after_second) == 4
    for pk, state in after_first.items():
        assert after_second[pk] == state, (
            "the second add restamped a line it did not create"
        )

    # Two configurations, two cids, and each fee line points at its own item.
    cids = {stamps[META_CID] for stamps, _ in after_second.values()}
    assert len(cids) == 2
    parents = [
        stamps[META_PARENT]
        for stamps, _ in after_second.values()
        if META_PARENT in stamps
    ]
    assert sorted(parents) == sorted(cids)


def test_a_configured_add_does_not_stamp_a_plain_retail_line_of_the_same_variant(
    client, checkout, stage_2_kit, omit_parts, crating_fee
):
    """A retail line already in the cart is not this request's to price."""
    option_set, values = omit_parts
    retail = checkout.lines.create(
        variant=stage_2_kit, quantity=1, currency=checkout.currency
    )

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [values[0].pk]}],
    )
    assert response.status_code == 200, response.content

    retail.refresh_from_db()
    assert retail.private_metadata == {}
    assert retail.price_override is None
    assert retail.price_override_reason is None


# --- (e) an id that is not an id, and a product that is not published -------


def post_raw(client, body):
    return client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(body),
        content_type="application/json",
        **HEADERS,
    )


def line_body(checkout, variant, option_set, value, **overrides):
    body = {
        "checkoutId": gid("Checkout", checkout.token),
        "channel": checkout.channel.slug,
        "productId": gid("Product", variant.product_id),
        "variantId": gid("ProductVariant", variant.pk),
        "quantity": 1,
        "selections": [{"set_id": option_set.pk, "value_ids": [value.pk]}],
        "acceptedFeeIds": [],
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkoutId", "garbage"),
        ("checkoutId", gid("Checkout", "not-a-uuid")),
        ("productId", gid("Product", "not-a-number")),
        ("variantId", "garbage"),
        ("customerId", gid("User", "not-a-number")),
    ],
)
def test_an_id_that_is_not_an_id_is_refused_rather_than_raised(
    client, checkout, stage_2_kit, omit_parts, field, value
):
    """A key-gated money endpoint answers 400, never 500.

    The pk inside a well-formed global id used to go straight to the ORM, so
    `base64("Checkout:junk")` reached `uuid.UUID()` and a bare `garbage` reached
    `int()`. A 500 is a stack trace in the log and a retry from the caller.
    """
    option_set, values = omit_parts

    response = post_raw(
        client,
        line_body(checkout, stage_2_kit, option_set, values[0], **{field: value}),
    )

    assert response.status_code == 400, response.content
    assert "malformed" in response.json()["violations"][0]


def test_the_option_sets_url_refuses_a_malformed_product_id(client):
    response = client.get(OPTION_SETS_URL.format("garbage"))

    assert response.status_code == 400, response.content


def test_option_sets_does_not_answer_for_an_unpublished_product(
    client, stage_2_kit, omit_parts, crating_fee
):
    """The one anonymous endpoint does not leak an unreleased product's configuration.

    Product ids are sequential integers inside a guessable global id, and an
    unreleased product's option prices are not PDP-visible the way a published
    product's are. Absent and unpublished answer identically, so this is not a
    row-existence oracle either.
    """
    url = OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id))
    assert client.get(url).status_code == 200

    stage_2_kit.product.channel_listings.update(is_published=False)

    hidden = client.get(url)
    absent = client.get(OPTION_SETS_URL.format(gid("Product", 987654321)))
    assert hidden.status_code == 404
    assert hidden.content == absent.content


def test_a_configured_add_refuses_a_buyer_the_checkout_does_not_belong_to(
    client, checkout, stage_2_kit, omit_parts, customer_user, staff_user
):
    """The checkout's own user is evidence about the buyer, and it used to be ignored."""
    option_set, values = omit_parts
    checkout.user = staff_user
    checkout.save(update_fields=["user"])

    response = post_raw(
        client,
        line_body(
            checkout,
            stage_2_kit,
            option_set,
            values[0],
            customerId=gid("User", customer_user.pk),
        ),
    )

    assert response.status_code == 409, response.content
    assert checkout.lines.count() == 0


def test_a_configured_add_still_serves_the_buyer_the_checkout_belongs_to(
    client, checkout, stage_2_kit, omit_parts, customer_user
):
    option_set, values = omit_parts
    checkout.user = customer_user
    checkout.save(update_fields=["user"])

    response = post_raw(
        client,
        line_body(
            checkout,
            stage_2_kit,
            option_set,
            values[0],
            customerId=gid("User", customer_user.pk),
        ),
    )

    assert response.status_code == 200, response.content


# --- (e) option-set parity wave A: help text, the default, the deselect prompt


@pytest.fixture
def tuner(stage_2_kit):
    """An OPTIONAL question whose pre-picked answer costs 250.00.

    The shape the fleet sweep counted 429 times: a default that moves the
    quoted price, not just the radio button.
    """
    option_set = OptionSet.objects.create(
        product=stage_2_kit.product,
        name="Tuner",
        label="Add a tuner",
        prompt_type="choice_one",
        required=False,
        deselect_prompt="Skip the tuner",
    )
    plain = OptionValue.objects.create(
        option_set=option_set,
        name="No tuner",
        price_delta=Decimal("0.00"),
        sort_order=0,
    )
    default = OptionValue.objects.create(
        option_set=option_set,
        name="Stage 2 tuner",
        sku_fragment="ST2",
        price_delta=Decimal("250.00"),
        help_text="Fits 2019 and newer only",
        is_default=True,
        sort_order=1,
    )
    return option_set, plain, default


def test_the_pdp_read_serves_the_help_text_the_default_and_the_prompt(
    client, stage_2_kit, tuner
):
    option_set, plain, default = tuner

    body = client.get(
        OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id))
    ).json()

    served = next(s for s in body["data"] if s["id"] == option_set.pk)
    assert served["deselect_prompt"] == "Skip the tuner"
    assert [(v["name"], v["help_text"], v["is_default"]) for v in served["values"]] == [
        ("No tuner", "", False),
        ("Stage 2 tuner", "Fits 2019 and newer only", True),
    ]


def test_an_add_that_never_mentions_the_tuner_is_charged_the_default(
    client, checkout, stage_2_kit, tuner
):
    """The money assertion: the CART line, not the response alone."""
    option_set, plain, default = tuner

    response = post_line(client, checkout, stage_2_kit, selections=[])

    assert response.status_code == 200, response.content
    line = checkout.lines.get()
    assert line.price_override == BASE_PRICE + Decimal("250.00")
    assert line.metadata[META_SKU].endswith("ST2")


def test_a_shopper_who_takes_the_deselect_option_pays_the_base_price(
    client, checkout, stage_2_kit, tuner
):
    """Skip the tuner posts the set with no values: said no, not said nothing."""
    option_set, plain, default = tuner

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": []}],
    )

    assert response.status_code == 200, response.content
    assert checkout.lines.get().price_override == BASE_PRICE


def test_a_shopper_who_picks_the_cheaper_answer_is_not_charged_the_default(
    client, checkout, stage_2_kit, tuner
):
    option_set, plain, default = tuner

    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [plain.pk]}],
    )

    assert response.status_code == 200, response.content
    assert checkout.lines.get().price_override == BASE_PRICE
