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

import datetime
import json
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from saleor.checkout.complete_checkout import create_order_from_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import CheckoutLine
from saleor.plugins.manager import get_plugins_manager
from saleor.wsm.compose.models import OptionValue
from saleor.wsm.compose.tests.test_api import (  # noqa: F401
    CONFIGURED_UNIT,
    FEE_AMOUNT,
    buckle,
    crating_fee,
    dealer_credit,
    gid,
    jobber,
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
from saleor.wsm.compose.lines import META_OPTIONS as OPTIONS_KEY
from saleor.wsm.dealer.no_stacking import LINE_METADATA_KEY as DEALER_KEY
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


# --- exploit 3: the dealer group a shopper writes for themselves ------------


LINES_ADD = """
    mutation($id: ID!, $variant: ID!, $quantity: Int!) {
      checkoutLinesAdd(id: $id, lines: [{variantId: $variant, quantity: $quantity}]) {
        errors { field message code }
      }
    }"""

UPDATE_METADATA = """
    mutation($id: ID!, $input: [MetadataInput!]!) {
      updateMetadata(id: $id, input: $input) {
        errors { field message code }
        item { metadata { key value } }
      }
    }"""

ADD_PROMO_CODE = """
    mutation($id: ID!, $code: String!) {
      checkoutAddPromoCode(id: $id, promoCode: $code) {
        errors { field message }
        checkout { lines {
          variant { id }
          totalPrice { gross { amount } }
        } }
      }
    }"""

READ_CHECKOUT = """
    query($id: ID!) {
      checkout(id: $id) { lines { unitPrice { gross { amount } } } }
    }"""


def graphql(client, query, variables):
    response = client.post(
        "/graphql/",
        data=json.dumps({"query": query, "variables": variables}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def test_a_dealer_stamp_a_shopper_wrote_for_themselves_buys_nothing(
    client, checkout, variant, tiers, channel_USD, stock,
):
    """Anonymous shopper stamps their own line with a dealer group code.

    Stock Saleor maps CheckoutLine PUBLIC metadata to `no_permissions`
    (saleor/graphql/meta/permissions.py), so `updateMetadata` on a line in your
    own checkout takes no key, no login and no dealer account. Group codes are
    short and guessable. Before the stamps moved to private metadata, MP3's
    anonymous branch read this key and priced the line at that group's ladder:
    10 units at 10.00 came back at 7.00.

    The forgery is asserted to SUCCEED. Closing this by refusing the write would
    mean patching a core permission map; it is closed instead by MP3 pricing
    from the private copy only, which no unauthenticated caller can write.
    """
    assert checkout.user is None
    errors = graphql(
        client,
        LINES_ADD,
        {
            "id": gid("Checkout", checkout.token),
            "variant": gid("ProductVariant", variant.pk),
            "quantity": 10,
        },
    )["checkoutLinesAdd"]["errors"]
    assert errors == [], errors
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)

    forged = graphql(
        client,
        UPDATE_METADATA,
        {
            "id": gid("CheckoutLine", line.pk),
            "input": [
                {"key": "wsm.dealer", "value": json.dumps({"group": tiers.code})}
            ],
        },
    )["updateMetadata"]
    assert forged["errors"] == [], "the public write is open, and that is the point"

    # The next price recalculation, which is every cart read once the checkout
    # goes stale. Nothing else about the line changed.
    checkout.price_expiration = timezone.now() - datetime.timedelta(hours=1)
    checkout.save(update_fields=["price_expiration"])
    read = graphql(client, READ_CHECKOUT, {"id": gid("Checkout", checkout.token)})

    assert read["checkout"]["lines"][0]["unitPrice"]["gross"]["amount"] == float(RETAIL)
    line.refresh_from_db()
    assert line.price_override is None
    assert line.price_override_reason is None
    assert "wsm.dealer" not in line.metadata, "the forged key is cleared, not kept"
    assert "wsm.dealer" not in line.private_metadata


# --- exploit 4: the dealer group a configured line quietly loses -------------


# The dealer's own price for the Stage 2 Kit configured with all three credits:
# 3998.99 less 29.99, 30.00 and the dealer's 545.00 in place of retail's 445.00.
DEALER_CONFIGURED = Decimal("3394.00")


def test_a_configured_dealer_line_keeps_its_group_on_an_anonymous_checkout(
    client, checkout, stage_2_kit, omit_parts, dealer_credit,
):
    """The storefront's normal shape: a dealer priced, no user on the checkout.

    The key-gated add resolves the customer server side and never attaches them,
    so `checkout_info.user` is None on every recalculation that follows. Before
    the stamped-group fallback, the first one repriced this line at RETAIL and
    rewrote the snapshot to match: 3494.00, silently, 100.00 more than the
    dealer agreed to and with nothing left on the line to say a tier ever
    applied. `_reprice_dealer` has had this fallback all along.
    """
    option_set, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        customer=dealer_credit,
    )
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == DEALER_CONFIGURED
    assert checkout.user is None, "the add attaches nobody, which is the point"

    checkout_info, lines = checkout_info_for(checkout)
    reprice(checkout_info, lines)

    line.refresh_from_db()
    assert line.price_override == DEALER_CONFIGURED
    assert json.loads(line.private_metadata[OPTIONS_KEY])["tier_applied"] is True


def test_a_signed_in_retail_shopper_cannot_inherit_a_stamped_group(
    client, checkout, stage_2_kit, omit_parts, dealer_credit, staff_user,
):
    """The fallback is for a checkout with NO buyer, never for the wrong one.

    Same line, same stamp, but the checkout now knows who is buying and it is
    not the dealer. The group on the line loses to the buyer on the checkout,
    and the price goes back to retail because this buyer really does pay retail.
    """
    option_set, values = omit_parts
    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        customer=dealer_credit,
    )
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == DEALER_CONFIGURED

    checkout.user = staff_user
    checkout.save(update_fields=["user"])
    checkout_info, lines = checkout_info_for(checkout)
    reprice(checkout_info, lines)

    line.refresh_from_db()
    assert line.price_override == Decimal(CONFIGURED_UNIT)


def test_a_voucher_does_not_stack_on_a_configured_line_that_took_a_tier(
    client, checkout, stage_2_kit, omit_parts, dealer_credit, voucher_percentage,
):
    """Better of, never both, on a configured line as much as a plain one.

    MP1 finds a dealer line by the presence of the `wsm.dealer` stamp and by
    nothing else. A configured line never carried it, so a SPECIFIC_PRODUCT
    voucher came straight off a price that was already the dealer's: 10 percent
    off 3394.00 is 3054.60, which is the stack this refuses.
    """
    from saleor.discount import VoucherType

    option_set, values = omit_parts
    voucher_percentage.type = VoucherType.SPECIFIC_PRODUCT
    voucher_percentage.save(update_fields=["type"])
    voucher_percentage.products.add(stage_2_kit.product)

    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        customer=dealer_credit,
    )
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert DEALER_KEY in line.private_metadata, "a tiered line says it is a dealer line"

    payload = graphql(
        client,
        ADD_PROMO_CODE,
        {"id": gid("Checkout", checkout.token), "code": voucher_percentage.codes.first().code},
    )["checkoutAddPromoCode"]
    assert payload["errors"] == [], payload["errors"]

    totals = {
        row["variant"]["id"]: row["totalPrice"]["gross"]["amount"]
        for row in payload["checkout"]["lines"]
    }
    assert totals[gid("ProductVariant", stage_2_kit.pk)] == float(DEALER_CONFIGURED)


# --- the cost, and the one case that refuses -------------------------------


def test_a_checkout_this_fork_does_not_own_costs_no_queries(checkout_with_items):
    """The claim in CORE-TOUCHES MP3, asserted rather than asserted-in-prose."""
    checkout_info, lines = checkout_info_for(checkout_with_items)

    with CaptureQueriesContext(connection) as captured:
        moved = reprice(checkout_info, lines)

    assert moved == []
    assert captured.captured_queries == []


def test_a_configured_checkout_costs_the_queries_the_doc_says_it_does(
    client, checkout, stage_2_kit, omit_parts, crating_fee, customer_user,
):
    """The claim in CORE-TOUCHES MP3, asserted rather than asserted-in-prose.

    This is the STEADY read: a configured line and its fee, priced already, read
    again with nothing moving. It is the cost every cart render pays, so the
    number in the doc has to be this one and not a guess. The doc said three.
    """
    configure(client, checkout, stage_2_kit, omit_parts)
    checkout_info, lines = checkout_info_for(checkout)

    with CaptureQueriesContext(connection) as captured:
        moved = reprice(checkout_info, lines)

    assert moved == [], "nothing moved, so nothing is written"
    tables = [q["sql"].split(" FROM ")[-1].split()[0] for q in captured.captured_queries]
    assert tables == [
        # The option sets on the configured products, with their values and the
        # dealer deltas on those values: one prefetch, three queries.
        '"wsm_compose_optionset"',
        '"wsm_compose_optionvalue"',
        '"wsm_compose_dealertieroptionprice"',
        # The fees on those same products.
        '"wsm_compose_fee"',
    ]

    # And the fifth, on a checkout that carries its buyer: their group, once for
    # the whole checkout. An anonymous one skips it, which is why the number is
    # a range and not a number.
    checkout.user = customer_user
    checkout.save(update_fields=["user"])
    checkout_info, lines = checkout_info_for(checkout)

    with CaptureQueriesContext(connection) as captured:
        reprice(checkout_info, lines)

    assert len(captured.captured_queries) == 5, [
        q["sql"][:90] for q in captured.captured_queries
    ]


def test_a_dealer_priced_checkout_costs_one_more_query_for_the_whole_cart(
    client, checkout, stage_2_kit, buckle, jobber,
):
    """The ladder that decides a dealer's base is read ONCE, not once per line.

    Same steady read as the test above with a dealer on it: the four catalog
    queries plus one ladder for every configured line at once. A per-line read
    here would be a query per cart line on the hottest path in the app, which is
    the shape `_reprice_dealer` was written to avoid.
    """
    option_set, value = buckle
    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
        customer=jobber,
    )
    checkout_info, lines = checkout_info_for(checkout)

    with CaptureQueriesContext(connection) as captured:
        moved = reprice(checkout_info, lines)

    assert moved == [], "nothing moved, so nothing is written"
    tables = [q["sql"].split(" FROM ")[-1].split()[0] for q in captured.captured_queries]
    assert tables == [
        '"wsm_compose_optionset"',
        '"wsm_compose_optionvalue"',
        '"wsm_compose_dealertieroptionprice"',
        '"wsm_compose_fee"',
        # The buyer's own price for the variants under the configured lines.
        '"wsm_dealer_tierprice"',
    ]


def test_a_price_correction_does_not_write_back_a_stale_quantity(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """This funnel owns the price on a line. It does not own the quantity.

    Core loads the line objects it hands us on the REPLICA, so their `quantity`
    is whatever that replica last saw. Writing the whole set of fields back
    meant a recalculation triggered by anything at all, a merchant editing what
    an option costs, silently undid a quantity the shopper had committed in
    another request moments earlier: they get charged for one when they asked
    for four, or for four when they cut back to one, with nothing in the cart
    saying it changed. It only takes replica lag, not a race.
    """
    parent, _ = configure(client, checkout, stage_2_kit, omit_parts)
    checkout_info, lines = checkout_info_for(checkout)
    # Committed by another request, after the line objects above were read.
    CheckoutLine.objects.filter(pk=parent.pk).update(quantity=4)
    # And the merchant edits an option price, so this line does have to move.
    OptionValue.objects.filter(pk=omit_parts[1][0].pk).update(
        price_delta=Decimal("-50.00")
    )

    reprice(checkout_info, lines)

    parent.refresh_from_db()
    assert parent.quantity == 4, "MP3 owns the price on this line, not the quantity"
    assert parent.price_override != CONFIGURED_UNIT, "the price correction still lands"


# --- the cart that can no longer be priced, and still reads -----------------


LINES_DELETE = """
    mutation($id: ID!, $lines: [ID!]!) {
      checkoutLinesDelete(id: $id, linesIds: $lines) {
        errors { field message code }
      }
    }"""


@pytest.fixture
def handling_fee(stage_2_kit):
    """A fee the shopper may decline, which the required crating fee is not."""
    from saleor.wsm.compose.models import Fee

    return Fee.objects.create(
        product=stage_2_kit.product,
        label="Handling",
        sku="HANDLE-01",
        basis="fixed",
        amount=Decimal("25.00"),
        apply_to="line",
        required=False,
    )


def ident(checkout):
    return {"id": gid("Checkout", checkout.token)}


def prices(read):
    return [row["unitPrice"]["gross"]["amount"] for row in read["checkout"]["lines"]]


def delete_lines(client, checkout, *lines):
    """The stock mutation any cart's remove button calls."""
    payload = graphql(
        client,
        LINES_DELETE,
        {
            "id": gid("Checkout", checkout.token),
            "lines": [gid("CheckoutLine", line.pk) for line in lines],
        },
    )["checkoutLinesDelete"]
    assert payload["errors"] == [], payload["errors"]


def configure(client, checkout, stage_2_kit, omit_parts, accepted=()):
    option_set, values = omit_parts
    response = post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [v.pk for v in values]}],
        accepted=[fee.pk for fee in accepted],
    )
    assert response.status_code == 200, response.content
    parent = checkout.lines.get(variant_id=stage_2_kit.pk)
    return parent, list(checkout.lines.exclude(pk=parent.pk))


def test_deleting_an_optional_fee_line_declines_it_and_keeps_the_item(
    client, checkout, stage_2_kit, omit_parts, handling_fee,
):
    """The wedge, and the shape of the answer.

    Every price recalculation runs through this funnel, and a recalculation is
    what a cart READ is. Raising here did not warn the shopper about one line,
    it made the whole checkout impossible to render, impossible to repair and
    impossible to empty, reachable with the stock remove button on a fee line.

    A charge the shopper was allowed to decline, declined the hard way, is a
    decline: the fee goes, the item stays, the cart reads.
    """
    parent, fees = configure(
        client, checkout, stage_2_kit, omit_parts, accepted=[handling_fee]
    )
    assert len(fees) == 1

    delete_lines(client, checkout, fees[0])
    read = graphql(client, READ_CHECKOUT, ident(checkout))

    assert prices(read) == [float(CONFIGURED_UNIT)]
    parent.refresh_from_db()
    assert parent.price_override == Decimal(CONFIGURED_UNIT)


def test_deleting_a_required_fee_line_takes_the_item_with_it(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """A required charge cannot be declined, so the item it belongs to goes.

    Leaving the parent would sell a crated item without the crate, which is the
    undercharge that deleting the line would otherwise buy. The cart still
    READS, which is the whole point of the policy; it just no longer holds an
    item this fork cannot price correctly.
    """
    parent, fees = configure(client, checkout, stage_2_kit, omit_parts)
    assert len(fees) == 1, "the crating fee is required, so it is charged unasked"

    delete_lines(client, checkout, fees[0])
    read = graphql(client, READ_CHECKOUT, {"id": gid("Checkout", checkout.token)})

    # The render that does the dropping had already loaded the line, so it draws
    # it one last time at nothing, which is what the total it shows says too.
    assert prices(read) == [0.0]
    assert not CheckoutLine.objects.filter(pk=parent.pk).exists()
    assert prices(graphql(client, READ_CHECKOUT, ident(checkout))) == []


def test_deleting_the_configured_parent_takes_its_charges_with_it(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """A crate with nothing to crate is not a thing anyone owes money for."""
    parent, fees = configure(client, checkout, stage_2_kit, omit_parts)

    delete_lines(client, checkout, parent)
    read = graphql(client, READ_CHECKOUT, ident(checkout))

    assert prices(read) == [0.0]
    assert not CheckoutLine.objects.filter(pk=fees[0].pk).exists()
    assert prices(graphql(client, READ_CHECKOUT, ident(checkout))) == []


def test_a_configured_line_whose_option_vanished_leaves_the_cart_readable(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """No correction can invent the right price, so the line stops existing.

    The merchant deleted a value a live cart was built on. Guessing at a price
    is worse than not selling it, and so is a checkout that 500s forever.
    """
    parent, fees = configure(client, checkout, stage_2_kit, omit_parts)
    OptionValue.objects.filter(pk=omit_parts[1][0].pk).delete()

    checkout_info, lines = checkout_info_for(checkout)
    reprice(checkout_info, lines)

    assert not CheckoutLine.objects.filter(pk__in=[parent.pk, fees[0].pk]).exists()
    assert prices(graphql(client, READ_CHECKOUT, ident(checkout))) == []


def test_a_promotion_that_starts_after_the_add_moves_the_line_down(
    client, checkout, stage_2_kit, omit_parts, crating_fee,
):
    """A sale the merchant starts mid-cart reaches the line on the next read.

    The mirror of every other correction in this file: the base price moved, so
    the configured line moves with it. Without it a shopper who loaded a page
    before the promotion went live pays the old price all the way into the order,
    while the catalog beside it quotes the new one.
    """
    parent, _ = configure(client, checkout, stage_2_kit, omit_parts)
    assert parent.price_override == Decimal(CONFIGURED_UNIT)

    stage_2_kit.channel_listings.filter(channel=checkout.channel).update(
        discounted_price_amount=Decimal("3500.00")
    )

    checkout_info, lines = checkout_info_for(checkout)
    reprice(checkout_info, lines)

    parent.refresh_from_db()
    # 3500.00 less the same three omitted parts.
    assert parent.price_override == Decimal("2995.01")


# --- exploit 6: the dealer base a quantity change could hand back to retail --


def test_a_dealers_configured_base_survives_the_stock_quantity_stepper(
    client, checkout, stage_2_kit, buckle, jobber,
):
    """The storefront's real shape: priced for a dealer, no user on the checkout.

    The add endpoint resolves the buyer server side and never attaches him, so
    every recalculation after it sees an anonymous checkout and re-derives the
    base from the group stamped on the line. If the funnel read the listing
    instead, the first cart render after a quantity change would quietly hand
    this dealer back to retail: 4006.99 where he agreed to 3208.00, with the
    snapshot rewritten to match so nothing on the line says a tier ever applied.
    """
    option_set, value = buckle
    post_line(
        client,
        checkout,
        stage_2_kit,
        selections=[{"set_id": option_set.pk, "value_ids": [value.pk]}],
        customer=jobber,
    )
    line = checkout.lines.get(variant_id=stage_2_kit.pk)
    assert line.price_override == Decimal("3208.00")
    assert checkout.user is None, "the add attaches nobody, which is the point"

    drop_quantity(client, checkout, line, 2)

    line.refresh_from_db()
    assert line.quantity == 2
    assert line.price_override == Decimal("3208.00")
    assert json.loads(line.private_metadata[OPTIONS_KEY])["tier_applied"] is True
    assert DEALER_KEY in line.private_metadata
