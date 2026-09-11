# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Placing an order on account: every refusal, and what the happy path leaves.

Driven the way a storefront drives it, through the public GraphQL endpoint as
the signed-in shopper, because the gate on this mutation is not a permission the
test framework can hand out: it is five questions about the caller, and a test
that called `perform_mutation` directly would answer none of them.

The money assertions are on the ORDER, never on the payload: an order line is
what gets invoiced, and a mutation that quotes the right number and writes the
wrong one is the failure this file exists to prevent. The tamper test is the
sharpest of them: the cart is edited in the database to a price nobody offered,
and the order still charges the dealer's 8.00, because the amount is re-derived
at the payment seam and never read out of the cart.
"""

import json
import uuid
from decimal import Decimal

import graphene
import pytest
from django.utils import timezone

from ....checkout.calculations import fetch_checkout_data
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....graphql.tests.utils import get_graphql_content
from ....order.models import Order
from ....payment import ChargeStatus  # noqa: F401  (documents the stock enum)
from ....payment.models import TransactionItem
from ....payment.transaction_item_calculations import recalculate_transaction_amounts
from ....payment.utils import create_manual_adjustment_events
from ....plugins.manager import get_plugins_manager
from .. import terms
from ..models import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_HOLD,
    DealerCustomer,
    DealerGroup,
    DealerSettings,
)
from ..no_stacking import LINE_METADATA_KEY as DEALER_KEY
from .test_views import (  # noqa: F401
    LINE_URL,
    dealer_group,
    gid,
    line_body,
    post,
    tiers,
)

pytestmark = pytest.mark.django_db

# The dealer ladder the `tiers` fixture writes: 8.00 each from five up.
DEALER_UNIT = Decimal("8.00")

# `saleor/tests/settings.py` sets `PLUGINS = []`, so a test that wants a plugin
# has to name it, and `test_the_hold_plugin_is_registered_in_the_settings_that_ship`
# is what proves the deployment loads it. Same pattern as
# `saleor/wsm/compose/tests/test_compliance.py`.
HOLD_PLUGIN_PATH = "saleor.wsm.dealer.plugin.DealerAccountHoldPlugin"
DEALER_QUANTITY = 6

COMPLETE_ON_TERMS = """
    mutation($id: ID!, $po: String) {
      wsmCheckoutCompleteOnTerms(checkoutId: $id, poNumber: $po) {
        order { id number }
        errors { field message code }
      }
    }"""


def call(api_client, checkout, po=None):
    """The mutation, as the storefront sends it. Payload only, never `.get`."""
    response = api_client.post_graphql(
        COMPLETE_ON_TERMS,
        {"id": graphene.Node.to_global_id("Checkout", checkout.pk), "po": po},
    )
    return get_graphql_content(response)["data"]["wsmCheckoutCompleteOnTerms"]


CHECKOUT_COMPLETE = """
    mutation($id: ID!) {
      checkoutComplete(id: $id) {
        order { id }
        errors { field message code }
      }
    }"""


def pay_by_card(checkout):
    """What a card app leaves behind: a transaction that CHARGED the total.

    Built with stock's own helpers rather than by setting columns, because a
    transaction's amounts are recomputed from its events; a row with a charged
    value and no event behind it reads as paid and reconciles to nothing.
    """
    manager = get_plugins_manager(allow_replica=False)
    lines, _unavailable = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    fetch_checkout_data(checkout_info, manager, lines, requestor=None).get()
    total = checkout_info.checkout.total.gross.amount
    charged = TransactionItem.objects.create(
        token=uuid.uuid4(),
        use_old_id=True,
        checkout_id=checkout.pk,
        currency=checkout.currency,
        name="Card",
    )
    create_manual_adjustment_events(
        transaction=charged,
        money_data={"charged_value": total},
        user=None,
        app=None,
    )
    recalculate_transaction_amounts(charged)
    fetch_checkout_data(
        checkout_info, manager, lines, requestor=None, force_status_update=True
    ).get()
    checkout.refresh_from_db()
    return charged


def refusal(payload):
    """The one error's code, asserting there is exactly one and no order."""
    assert payload["order"] is None, "a refused checkout must place no order"
    assert len(payload["errors"]) == 1, payload["errors"]
    return payload["errors"][0]["code"]


@pytest.fixture
def dealer_cart(
    client, checkout, variant, customer_user, tiers, channel_USD, stock, address
):
    """A signed-in dealer's checkout with a dealer-priced line, ready to place.

    The line is added through the fork's own dealer endpoint rather than written
    by hand, so the price on it is the one the live path produces.
    """
    response = post(
        client,
        LINE_URL,
        line_body(checkout, customer_user, variant, channel_USD, DEALER_QUANTITY),
    )
    assert response.status_code == 200, response.content
    checkout.refresh_from_db()
    checkout.user = customer_user
    checkout.email = customer_user.email
    checkout.shipping_address = address
    checkout.billing_address = address
    checkout.save()
    return checkout


@pytest.fixture
def placeable(dealer_cart, checkout_delivery):
    """The same cart with a delivery method chosen, which completion requires."""
    dealer_cart.assigned_delivery = checkout_delivery(dealer_cart)
    dealer_cart.save()
    return dealer_cart


@pytest.fixture
def hold_plugin(settings):
    settings.PLUGINS = [HOLD_PLUGIN_PATH]
    return HOLD_PLUGIN_PATH


def test_the_hold_plugin_is_registered_in_the_settings_that_ship(settings):
    """The seam is only a seam if the deployment loads it."""
    assert HOLD_PLUGIN_PATH in settings.BUILTIN_PLUGINS


@pytest.fixture
def on_terms(dealer_group, customer_user):  # noqa: F811
    """Turn the account's invoice payment ON, which is never the default."""
    account = DealerCustomer.objects.get(user=customer_user)
    account.invoice_payment = True
    account.account_number = "DS-4471"
    account.save()
    return account


# --- the refusals ------------------------------------------------------------


def test_a_dealer_the_merchant_has_not_enabled_is_refused(
    user_api_client,
    placeable,
    dealer_group,  # noqa: F811
):
    """Default deny: a dealer record alone buys nothing on terms."""
    assert DealerCustomer.objects.get().invoice_payment is False
    assert refusal(call(user_api_client, placeable)) == "TERMS_NOT_ENABLED"
    assert not Order.objects.exists()


def test_a_shopper_with_no_dealer_record_at_all_is_refused(
    user_api_client,
    placeable,
    dealer_group,  # noqa: F811
):
    DealerCustomer.objects.all().delete()
    assert refusal(call(user_api_client, placeable)) == "TERMS_NOT_ENABLED"


def test_an_anonymous_caller_is_refused(api_client, placeable, on_terms):
    """Not signed in is not the owner, whoever the cart belongs to."""
    assert refusal(call(api_client, placeable)) == "NOT_CHECKOUT_OWNER"
    assert not Order.objects.exists()


def test_another_shoppers_cart_is_refused(
    staff_api_client, placeable, on_terms, staff_user
):
    """Signed in, enabled for terms, and still not this cart's owner.

    The staff account is given its own terms-enabled dealer record first, so the
    only thing left that can refuse this call is the ownership test.
    """
    DealerCustomer.objects.create(
        user=staff_user,
        group=DealerGroup.objects.get(),
        invoice_payment=True,
    )
    assert refusal(call(staff_api_client, placeable)) == "NOT_CHECKOUT_OWNER"
    assert not Order.objects.exists()


def test_an_anonymous_cart_is_refused_even_to_a_terms_dealer(
    user_api_client, placeable, on_terms
):
    placeable.user = None
    placeable.save()
    assert refusal(call(user_api_client, placeable)) == "NOT_CHECKOUT_OWNER"


def test_a_missing_po_is_refused_only_when_the_merchant_demands_one(
    user_api_client, placeable, on_terms
):
    DealerSettings.objects.create(po_required=True)
    payload = call(user_api_client, placeable, po="   ")
    assert refusal(payload) == "PO_REQUIRED"
    assert payload["errors"][0]["field"] == "poNumber"
    assert not Order.objects.exists()


def test_no_po_is_asked_for_by_default(user_api_client, placeable, on_terms):
    """The same call with the setting left alone places the order."""
    assert DealerSettings.po_required_for_store() is False
    payload = call(user_api_client, placeable)
    assert payload["errors"] == []
    assert payload["order"] is not None


def test_an_account_on_hold_is_refused(user_api_client, placeable, on_terms):
    on_terms.account_status = ACCOUNT_STATUS_HOLD
    on_terms.save()
    assert refusal(call(user_api_client, placeable, po="DS-TEST-001")) == (
        "ACCOUNT_ON_HOLD"
    )
    assert not Order.objects.exists()


def test_an_account_on_hold_cannot_place_a_card_order_either(
    user_api_client, placeable, on_terms, hold_plugin
):
    """Hold is not a terms rule, so it is enforced on the path every order takes.

    Nothing about terms is involved: the card is charged, the storefront calls
    STOCK `checkoutComplete`, and the refusal comes from the plugin. The account
    has invoice payment turned OFF here, so the terms mutation could not have
    been the thing that said no.

    The seam matters and is measured, not assumed: `preprocess_order_creation`
    is called from `_prepare_order_data` (the Payment flow),
    `_prepare_checkout_with_transactions` (the transaction flow, which is this
    test) and `checkout_cleaner` (`orderCreateFromCheckout`). Calling
    `create_order_from_checkout` DIRECTLY reaches none of them, which is why
    that is not what this drives.
    """
    on_terms.invoice_payment = False
    on_terms.account_status = ACCOUNT_STATUS_HOLD
    on_terms.save()
    pay_by_card(placeable)

    payload = get_graphql_content(
        user_api_client.post_graphql(
            CHECKOUT_COMPLETE,
            {"id": graphene.Node.to_global_id("Checkout", placeable.pk)},
        )
    )["data"]["checkoutComplete"]

    assert payload["order"] is None
    assert "on hold" in " ".join(e["message"] for e in payload["errors"])
    assert not Order.objects.exists()


def test_an_active_account_is_not_held_up_by_the_plugin(
    user_api_client, placeable, on_terms, hold_plugin
):
    """The other half: the plugin refuses a hold and nothing else.

    Same card path, same account, one field different. Without this the hold
    test would pass just as well against a plugin that refused every order.
    """
    on_terms.invoice_payment = False
    on_terms.save()
    assert on_terms.account_status == ACCOUNT_STATUS_ACTIVE
    pay_by_card(placeable)

    payload = get_graphql_content(
        user_api_client.post_graphql(
            CHECKOUT_COMPLETE,
            {"id": graphene.Node.to_global_id("Checkout", placeable.pk)},
        )
    )["data"]["checkoutComplete"]

    assert payload["errors"] == []
    assert payload["order"] is not None
    assert Order.objects.get().charge_status == "full"


# --- the happy path ----------------------------------------------------------


def test_the_order_is_placed_unpaid_at_dealer_prices_with_the_po_on_it(
    user_api_client, placeable, on_terms
):
    payload = call(user_api_client, placeable, po="DS-TEST-001")
    assert payload["errors"] == []

    order = Order.objects.get()
    assert payload["order"]["number"] == str(order.number)

    # The lines still carry the dealer's price: terms are a payment method the
    # account enables ALONGSIDE dealer pricing, never instead of it.
    line = order.lines.get()
    assert line.quantity == DEALER_QUANTITY
    assert line.unit_price_net_amount == DEALER_UNIT
    assert DEALER_KEY in line.private_metadata, (
        "the dealer re-derivation's own stamp, copied from the checkout line"
    )
    assert json.loads(line.private_metadata[DEALER_KEY])["group"] == "dealer-1"

    # Unpaid, and authorized for the whole total: stock's own "charge later".
    assert order.authorize_status == "full"
    assert order.charge_status == "none"
    assert order.total_charged_amount == Decimal(0)
    assert order.total_authorized_amount == order.total_gross_amount

    transaction_item = TransactionItem.objects.get(order_id=order.pk)
    assert transaction_item.name == terms.TRANSACTION_NAME
    assert transaction_item.authorized_value == order.total_gross_amount
    assert transaction_item.charged_value == Decimal(0)

    # What both sides read the arrangement off.
    assert order.metadata[terms.TERMS_KEY] == terms.TERMS_ON_ACCOUNT
    assert order.metadata[terms.PO_KEY] == "DS-TEST-001"
    assert order.metadata[terms.ACCOUNT_NUMBER_KEY] == "DS-4471"
    assert order.private_metadata[terms.PRIVATE_PO_KEY] == "DS-TEST-001"
    assert order.private_metadata[terms.PRIVATE_TERMS_KEY] == terms.TERMS_ON_ACCOUNT

    # And the cart is gone, exactly as it is after a card order.
    assert not CheckoutLine.objects.filter(checkout_id=placeable.pk).exists()


def test_the_amount_authorized_is_re_derived_and_not_read_out_of_the_cart(
    user_api_client, placeable, on_terms
):
    """A tampered cart price does not reach the invoice.

    The line is edited in the database to a price no tier offers, the way a
    stolen storefront key or a leaked endpoint would edit it, and the prices are
    marked stale so the next read recalculates. The order still charges 8.00,
    because MP3 re-derives every fork-owned price inside the same
    `fetch_checkout_data` the authorized amount is taken from.
    """
    line = CheckoutLine.objects.get(checkout_id=placeable.pk)
    line.price_override = Decimal("1.00")
    line.save(update_fields=["price_override"])
    placeable.price_expiration = timezone.now()
    placeable.save(update_fields=["price_expiration"])

    payload = call(user_api_client, placeable, po="DS-TEST-001")
    assert payload["errors"] == []

    order = Order.objects.get()
    order_line = order.lines.get()
    assert order_line.unit_price_net_amount == DEALER_UNIT
    assert order.total_authorized_amount == order.total_gross_amount
    assert order.total_gross_amount > Decimal("6.00"), (
        "a 1.00 unit price would have invoiced 6.00 for the goods"
    )


def test_a_refused_completion_leaves_no_authorization_behind(
    user_api_client, dealer_cart, on_terms
):
    """No delivery method, so completion fails AFTER the authorization is made.

    The transaction is written before `complete_checkout` because that is the
    state stock refuses to complete without. If it survived a refusal the cart
    would read as paid for and stock's own automatic completion would try again.
    """
    payload = call(user_api_client, dealer_cart, po="DS-TEST-001")
    assert payload["order"] is None
    # A stock code the WsmError enum cannot say, reaching the shopper as the
    # sentence stock wrote rather than as a protocol error with no field.
    assert [error["code"] for error in payload["errors"]] == ["INVALID"]
    assert "shipping method" in payload["errors"][0]["message"].lower()

    assert not TransactionItem.objects.exists()
    dealer_cart.refresh_from_db()
    assert dealer_cart.authorize_status == "none"
    assert not Order.objects.exists()


# --- what the shopper is allowed to ask about themselves ---------------------

MY_TERMS = """
    query {
      wsmMyDealerTerms {
        invoicePayment
        accountNumber
        accountStatus
        poRequired
      }
    }"""


def my_terms(api_client):
    return get_graphql_content(api_client.post_graphql(MY_TERMS))["data"][
        "wsmMyDealerTerms"
    ]


def test_a_shopper_who_is_not_signed_in_is_told_nothing(api_client, on_terms):
    """Null, not an error: the storefront asks this on every checkout."""
    assert my_terms(api_client) is None


def test_a_shopper_with_no_dealer_record_is_told_nothing(
    user_api_client, customer_user
):
    assert my_terms(user_api_client) is None


def test_a_dealer_reads_their_own_flags_and_the_store_setting(
    user_api_client, on_terms
):
    DealerSettings.objects.create(po_required=True)
    assert my_terms(user_api_client) == {
        "invoicePayment": True,
        "accountNumber": "DS-4471",
        "accountStatus": "ACTIVE",
        "poRequired": True,
    }


def test_a_dealer_reads_their_OWN_record_and_not_the_other_ones(
    staff_api_client, on_terms, staff_user
):
    """No argument to pass, so there is nothing to point at someone else.

    Two terms dealers exist here and the answer names the caller's account
    number. That is the whole permission model of this query, and it is the one
    thing worth a test: every other dealer read in this layer is behind
    MANAGE_DISCOUNTS precisely because it answers about somebody else.
    """
    DealerCustomer.objects.create(
        user=staff_user,
        group=DealerGroup.objects.get(),
        invoice_payment=False,
        account_number="STAFF-1",
    )
    assert my_terms(staff_api_client) == {
        "invoicePayment": False,
        "accountNumber": "STAFF-1",
        "accountStatus": "ACTIVE",
        "poRequired": False,
    }


def test_a_held_account_says_so_before_the_shopper_reaches_the_payment_step(
    user_api_client, on_terms
):
    """The address step reads this, the way the restriction refusal does."""
    on_terms.account_status = ACCOUNT_STATUS_HOLD
    on_terms.save()
    assert my_terms(user_api_client)["accountStatus"] == "HOLD"


# --- the dealer's own account number, on the order --------------------------


def test_the_dealers_account_number_lands_on_the_order_a_merchant_reads(
    user_api_client, placeable, on_terms
):
    """Ds carries an account number on 504 orders; the fleet on 233,613.

    Both copies, and they are not the same thing. The PUBLIC one is the
    shopper's receipt. The PRIVATE one is what an invoice and the Dashboard
    order page read, because a shopper can write public metadata on their own
    CHECKOUT and stock copies a checkout's metadata onto the order at creation.
    """
    payload = call(user_api_client, placeable, po="DS-TEST-001")
    assert payload["errors"] == []

    order = Order.objects.get()
    assert order.metadata[terms.ACCOUNT_NUMBER_KEY] == "DS-4471"
    assert order.private_metadata[terms.PRIVATE_ACCOUNT_NUMBER_KEY] == "DS-4471"


def test_an_account_with_no_number_stamps_neither_copy(
    user_api_client, placeable, on_terms
):
    """An empty string is not an account number, and an empty key is worse."""
    on_terms.account_number = ""
    on_terms.save()

    assert call(user_api_client, placeable)["errors"] == []

    order = Order.objects.get()
    assert terms.ACCOUNT_NUMBER_KEY not in order.metadata
    assert terms.PRIVATE_ACCOUNT_NUMBER_KEY not in order.private_metadata
