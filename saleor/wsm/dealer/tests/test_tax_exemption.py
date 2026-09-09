# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One checkbox on a dealer account, and the tax it stops the shopper paying.

Every assertion here is on stock Saleor's own artifacts, `Checkout.tax_exemption`
and the tax on an ORDER line, because those are what a merchant and an auditor
look at. Nothing asserts that our helper ran.

The exploit these tests fence off is the obvious one: `customerId` arrives in a
request body, so tax-free shopping would be a body away if the routes that read
it were open. They are not (`saleor/wsm/http.py`), and the two tests at the end
say so from both directions: a caller cannot hand themselves the exemption
through the stock GraphQL API either, and a stamped line on an anonymous cart
gets its dealer PRICE re-derived and no exemption at all.
"""

import json
from decimal import Decimal

import pytest

from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....plugins.manager import get_plugins_manager
from ...reprice import reprice
# The one definition of "place the order this checkout is holding", already
# written for the MP3 suite. A second copy here would be a second thing to keep
# in step with `create_order_from_checkout`.
from ...tests.test_reprice import complete
from ..no_stacking import LINE_METADATA_KEY, PRICE_OVERRIDE_REASON
from ..models import DealerCustomer
from .test_views import (  # noqa: F401
    LINE_URL,
    REPRICE_URL,
    dealer_group,
    gid,
    line_body,
    post,
    tiers,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def exempt_dealer(dealer_group):  # noqa: F811
    """The same dealer the endpoint tests use, with the merchant's box ticked."""
    customer = DealerCustomer.objects.get(group=dealer_group)
    customer.tax_exempt = True
    customer.save(update_fields=["tax_exempt"])
    return customer


def add_five(client, checkout, customer_user, variant, channel_USD):
    """Five of the tiered variant, through the route the storefront calls."""
    response = post(
        client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 5)
    )
    assert response.status_code == 200, response.content
    return response


# --- the cart --------------------------------------------------------------


def test_a_tax_exempt_dealer_stops_paying_tax_the_moment_they_add_to_their_cart(
    client, checkout, variant, customer_user, tiers, exempt_dealer, channel_USD, stock
):
    add_five(client, checkout, customer_user, variant, channel_USD)

    checkout.refresh_from_db()
    assert checkout.tax_exemption is True


def test_a_dealer_without_the_exemption_is_taxed_like_any_other_shopper(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    add_five(client, checkout, customer_user, variant, channel_USD)

    checkout.refresh_from_db()
    assert checkout.tax_exemption is False


def test_revoking_the_exemption_takes_it_off_a_cart_the_shopper_is_already_holding(
    client, checkout, variant, customer_user, tiers, exempt_dealer, channel_USD, stock
):
    response = add_five(client, checkout, customer_user, variant, channel_USD)
    checkout.refresh_from_db()
    assert checkout.tax_exemption is True

    exempt_dealer.tax_exempt = False
    exempt_dealer.save(update_fields=["tax_exempt"])

    reprice_response = post(
        client,
        REPRICE_URL,
        {
            "checkoutId": gid("Checkout", checkout.pk),
            "channel": channel_USD.slug,
            "lineId": response.json()["lineId"],
            "customerId": gid("User", customer_user.pk),
        },
    )

    assert reprice_response.status_code == 200, reprice_response.content
    checkout.refresh_from_db()
    assert checkout.tax_exemption is False


def test_a_shopper_with_no_dealer_account_keeps_the_exemption_a_merchant_gave_by_hand(
    client, checkout, variant, customer_user, channel_USD, stock
):
    """A retail shopper is not this app's to answer for.

    Staff exempt a one-off buyer through `taxExemptionManage` and the shopper
    then carries on shopping. Writing False for everyone we do not recognise
    would undo that on their next add to cart, silently and for money.
    """
    checkout.tax_exemption = True
    checkout.save(update_fields=["tax_exemption"])

    add_five(client, checkout, customer_user, variant, channel_USD)

    checkout.refresh_from_db()
    assert checkout.tax_exemption is True


def test_an_anonymous_cart_carrying_a_dealer_stamp_is_still_charged_tax(
    checkout, variant, tiers, channel_USD
):
    """The stamp is a PRICE input and never an identity.

    The line below is what a forger would leave behind: a group name in the
    metadata MP3 re-prices from, on a checkout with no customer on it. MP3
    honours it for the price, on purpose, and the exemption does not follow,
    because the only writer of the flag is the key-gated route.
    """
    assert checkout.user is None
    line = CheckoutLine.objects.create(
        checkout=checkout,
        variant=variant,
        quantity=10,
        currency=channel_USD.currency_code,
        price_override=Decimal("999.00"),
        price_override_reason=PRICE_OVERRIDE_REASON,
        undiscounted_unit_price_amount=Decimal("999.00"),
        private_metadata={
            LINE_METADATA_KEY: json.dumps({"group": "dealer-1", "minQuantity": 10})
        },
    )

    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    reprice(fetch_checkout_info(checkout, lines, manager), lines)

    line.refresh_from_db()
    assert line.price_override == Decimal("7.000")
    checkout.refresh_from_db()
    assert checkout.tax_exemption is False


# --- the order -------------------------------------------------------------


def taxes_on(order):
    """(net, gross) per order line, which is where a tax is visible or absent."""
    return [
        (line.total_price_net_amount, line.total_price_gross_amount)
        for line in order.lines.all()
    ]


def test_an_exempt_dealers_order_is_placed_with_no_tax_on_a_single_line(
    client,
    checkout,
    variant,
    customer_user,
    tiers,
    exempt_dealer,
    channel_USD,
    stock,
    address,
    checkout_delivery,
    app,
    tax_configuration_flat_rates,
):
    add_five(client, checkout, customer_user, variant, channel_USD)

    order = complete(checkout, address, checkout_delivery, app)

    assert order.tax_exemption is True
    assert taxes_on(order) == [(Decimal("40.00"), Decimal("40.00"))]


def test_the_same_order_carries_tax_when_the_dealer_is_not_exempt(
    client,
    checkout,
    variant,
    customer_user,
    tiers,
    channel_USD,
    stock,
    address,
    checkout_delivery,
    app,
    tax_configuration_flat_rates,
):
    add_five(client, checkout, customer_user, variant, channel_USD)

    order = complete(checkout, address, checkout_delivery, app)

    assert order.tax_exemption is False
    net, gross = taxes_on(order)[0]
    assert net == Decimal("40.00")
    assert gross > net


# --- what a caller cannot do for themselves --------------------------------


TAX_EXEMPTION_MANAGE = """
    mutation($id: ID!) {
      taxExemptionManage(id: $id, taxExemption: true) {
        errors { field message code }
      }
    }"""


def test_a_shopper_cannot_hand_themselves_the_exemption_through_the_api(
    user_api_client, checkout
):
    """Stock Saleor's own answer, asserted so a rebase cannot quietly change it.

    `TaxExemptionManage` is gated on `MANAGE_TAXES` and no checkout mutation
    takes `taxExemption` as an input, so the flag has exactly two writers: staff
    with that permission, and the key-gated dealer routes.
    """
    response = user_api_client.post_graphql(
        TAX_EXEMPTION_MANAGE, {"id": gid("Checkout", checkout.pk)}
    )

    assert response.json()["data"]["taxExemptionManage"] is None
    checkout.refresh_from_db()
    assert checkout.tax_exemption is False
