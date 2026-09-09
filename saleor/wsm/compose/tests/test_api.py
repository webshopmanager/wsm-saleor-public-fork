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

from saleor.wsm.compose.models import Fee, OptionSet, OptionValue
from saleor.wsm.tests import COMPOSE_HEADERS
from saleor.wsm.compose.views import (
    META_CID,
    META_FEE,
    META_OPTIONS,
    META_PARENT,
    META_SKU,
    PRICE_OVERRIDE_REASON,
)

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


def test_option_sets_returns_the_contract_shape(client, stage_2_kit, omit_parts, crating_fee):
    option_set, values = omit_parts

    response = client.get(OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id)))

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
            "values": [
                {
                    "id": values[0].pk,
                    "name": "Omit fuel filter",
                    "sku_fragment": "NOFF",
                    "price_delta": "-29.99",
                    "image_url": "",
                },
                {
                    "id": values[1].pk,
                    "name": "Omit fuel lines",
                    "sku_fragment": "NOFL",
                    "price_delta": "-30.00",
                    "image_url": "",
                },
                {
                    "id": values[2].pk,
                    "name": "Omit pump assembly",
                    "sku_fragment": "NOPA",
                    "price_delta": "-445.00",
                    "image_url": "",
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


def test_option_sets_accepts_a_gid_that_lost_its_padding(client, stage_2_kit, omit_parts):
    raw = gid("Product", stage_2_kit.product_id).rstrip("=")

    assert client.get(OPTION_SETS_URL.format(raw)).status_code == 200


def test_option_sets_unknown_product_is_404(client, db):
    response = client.get(OPTION_SETS_URL.format(gid("Product", 999999)))

    assert response.status_code == 404
    assert response.json() == {"violations": ["unknown product"]}


def test_option_sets_stays_inside_its_query_budget(
    client, stage_2_kit, omit_parts, crating_fee, django_assert_num_queries
):
    # Design section 5: one read for the sets and their values (a prefetch is
    # two statements), one for the fees. Anything more is an N+1 creeping in.
    with django_assert_num_queries(3):
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
    fee_listing = fee_line.variant.product.channel_listings.get(channel=checkout.channel)
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
    post_line(client, checkout, stage_2_kit, selections=selections,
              accepted=[crating_fee.pk])

    with CaptureQueriesContext(connection) as captured:
        post_line(client, checkout, stage_2_kit, selections=selections,
                  accepted=[crating_fee.pk])

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


def test_option_sets_quotes_the_dealer_the_delta_it_will_charge(
    client, stage_2_kit, omit_parts, dealer_credit
):
    """Endpoint 1 with a customer id: the PDP shows what the add will take."""
    url = OPTION_SETS_URL.format(gid("Product", stage_2_kit.product_id))

    retail = client.get(url).json()["data"][0]["values"]
    dealer = client.get(url, {"customerId": gid("User", dealer_credit.pk)}).json()[
        "data"
    ][0]["values"]

    assert [v["price_delta"] for v in retail] == ["-29.99", "-30.00", "-445.00"]
    assert [v["price_delta"] for v in dealer] == ["-29.99", "-30.00", "-545.00"]



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
