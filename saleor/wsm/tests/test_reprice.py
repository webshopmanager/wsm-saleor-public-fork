# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""MP3: no price this fork wrote outlives the facts it was computed from.

Both exploits below are driven the way a shopper drives them: our own HTTP
endpoint puts the line on the checkout, then the STOCK `checkoutLinesUpdate`
mutation changes the quantity, and nothing calls our reprice endpoint. The
assertions are on the ORDER, because an order line is the artifact that gets
charged; a corrected checkout that still writes the old number into the order
is the failure this unit exists to prevent.

The fixtures are imported from the two endpoint test modules rather than copied,
so there is exactly one definition of the Stage 2 Kit and one of the dealer
ladder, and a drift in either reddens both suites at once.
"""

import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from saleor.checkout.complete_checkout import create_order_from_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import CheckoutLine
from saleor.plugins.manager import get_plugins_manager
from saleor.wsm.compose.models import OptionValue
from saleor.wsm.compose.tests.test_api import (  # noqa: F401
    CONFIGURED_UNIT,
    FEE_AMOUNT,
    crating_fee,
    gid,
    omit_parts,
    post_line,
    stage_2_kit,
)
from saleor.wsm.dealer.tests.test_views import (  # noqa: F401
    LINE_URL as DEALER_LINE_URL,
    dealer_group,
    line_body,
    post,
    tiers,
)
from saleor.wsm.reprice import reprice

pytestmark = pytest.mark.django_db

# What the `variant` fixture's USD listing costs anyone who is not a dealer.
RETAIL = Decimal("10.00")

LINES_UPDATE = """
    mutation($id: ID!, $line: ID!, $quantity: Int!) {
      checkoutLinesUpdate(id: $id, lines: [{lineId: $line, quantity: $quantity}]) {
        errors { field message code }
        checkout { lines { quantity unitPrice { gross { amount } } } }
      }
    }"""


def drop_quantity(client, checkout, line, quantity):
    """The quantity stepper every cart has, and nothing else."""
    response = client.post(
        "/graphql/",
        data=json.dumps(
            {
                "query": LINES_UPDATE,
                "variables": {
                    "id": gid("Checkout", checkout.token),
                    "line": gid("CheckoutLine", line.pk),
                    "quantity": quantity,
                },
            }
        ),
        content_type="application/json",
    )
    payload = response.json()["data"]["checkoutLinesUpdate"]
    assert payload["errors"] == [], payload["errors"]
    return payload


def complete(checkout, address, checkout_delivery, app):
    checkout.refresh_from_db()
    checkout.shipping_address = address
    checkout.billing_address = address
    checkout.assigned_delivery = checkout_delivery(checkout)
    checkout.save()
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    return create_order_from_checkout(
        checkout_info=checkout_info, manager=manager, user=None, app=app
    )


def checkout_info_for(checkout):
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


# --- exploit 1: the dealer tier that outlives its quantity ------------------


def test_a_tier_price_does_not_survive_the_quantity_drop_that_ends_it(
    client, checkout, variant, customer_user, tiers, channel_USD, stock,
    address, checkout_delivery, app,
):
    """Add 10 at the 10-break, drop to 1, complete: the order says retail.

    The buyer really is a dealer and the group really does resolve, so the only
    reason the price moves back is the quantity. Without MP3 the order line
    carries 7.00, which is the whole exploit.
    """
    checkout.user = customer_user
    checkout.email = customer_user.email
    checkout.save(update_fields=["user", "email"])

    response = post(
        client, DEALER_LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 10)
    )
    assert response.status_code == 200
    assert response.json()["dealerPrice"] == "7.00"
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    assert line.price_override == Decimal("7.00")

    drop_quantity(client, checkout, line, 1)

    # Read before completing: completion consumes the checkout and its lines.
    line.refresh_from_db()
    cart_override, cart_quantity = line.price_override, line.quantity

    order = complete(checkout, address, checkout_delivery, app)

    order_line = order.lines.get(variant_id=variant.pk)
    assert order_line.quantity == 1
    assert order_line.base_unit_price_amount == RETAIL
    assert order_line.total_price_gross_amount == RETAIL
    # The cart already said the same thing. Asserted second so that the ORDER is
    # what reddens when the enforcement is taken away.
    assert (cart_override, cart_quantity) == (None, 1)


def test_a_tier_price_falls_to_the_lower_break_on_an_anonymous_checkout(
    client, checkout, variant, customer_user, tiers, channel_USD, stock,
):
    """10 to 6 is still a dealer quantity: 7.00 becomes 8.00, not retail.

    No user on the checkout, which is how the storefront actually calls these
    endpoints: the group comes off the line's own stamp. The BREAK still moves.
    """
    assert checkout.user is None
    post(client, DEALER_LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 10))
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)

    drop_quantity(client, checkout, line, 6)

    line.refresh_from_db()
    assert line.price_override == Decimal("8.00")


# --- exploit 2: the per-unit fee that outlives its parent -------------------


def test_a_per_unit_fee_line_follows_its_parent_quantity_into_the_order(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
    address, checkout_delivery, app,
):
    """Configure 2, drop the parent to 1, complete: one crate, not two."""
    option_set, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        quantity=2,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        accepted=[crating_fee.pk],
    )
    parent = checkout.lines.get(variant_id=stage_2_kit.pk)
    fee_line = checkout.lines.exclude(pk=parent.pk).get()
    assert fee_line.quantity == 2

    drop_quantity(client, checkout, parent, 1)

    # Read before completing: completion consumes the checkout and its lines.
    fee_line.refresh_from_db()
    cart_fee_quantity = fee_line.quantity

    order = complete(checkout, address, checkout_delivery, app)

    parent_order_line = order.lines.get(variant_id=stage_2_kit.pk)
    fee_order_line = order.lines.exclude(pk=parent_order_line.pk).get()
    assert parent_order_line.quantity == 1
    assert parent_order_line.base_unit_price_amount == Decimal(CONFIGURED_UNIT)
    assert fee_order_line.quantity == 1, "one unit, so one crate on the order"
    assert fee_order_line.base_unit_price_amount == Decimal(FEE_AMOUNT)
    assert fee_order_line.total_price_gross_amount == Decimal(FEE_AMOUNT)
    # Same again: the cart was already right, but the ORDER is the assertion
    # that has to fail when the enforcement is taken away.
    assert cart_fee_quantity == 1


def test_a_per_unit_fee_line_follows_its_parent_upwards_too(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    option_set, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        quantity=2,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        accepted=[crating_fee.pk],
    )
    parent = checkout.lines.get(variant_id=stage_2_kit.pk)
    fee_line = checkout.lines.exclude(pk=parent.pk).get()

    drop_quantity(client, checkout, parent, 3)

    fee_line.refresh_from_db()
    assert fee_line.quantity == 3


# --- the cost, and the one case that refuses -------------------------------


def test_a_checkout_this_fork_does_not_own_costs_no_queries(checkout_with_items):
    """The claim in CORE-TOUCHES MP3, asserted rather than asserted-in-prose."""
    checkout_info, lines = checkout_info_for(checkout_with_items)

    with CaptureQueriesContext(connection) as captured:
        moved = reprice(checkout_info, lines)

    assert moved == []
    assert captured.captured_queries == []


def test_a_configured_line_whose_option_vanished_refuses_rather_than_guesses(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """No correction can invent the right price, so the cart says so."""
    option_set, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        accepted=[crating_fee.pk],
    )
    OptionValue.objects.filter(pk=values[0].pk).delete()

    checkout_info, lines = checkout_info_for(checkout)

    with pytest.raises(ValidationError) as raised:
        reprice(checkout_info, lines)
    assert "lines" in raised.value.error_dict
