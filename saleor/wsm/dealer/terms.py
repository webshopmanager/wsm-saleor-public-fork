# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Checking out on account: who may, what it stamps, and how stock gets paid.

ds (Diversified Shafts Solutions) does not pay by card. An approved dealer
places the order, names a purchase order number, and the merchant invoices.
On 5.0 that is 108 tenants and $405M TTM, and a PO number at checkout is 27 of
them (`dealer-account-requirements-2026-09-02`).

**Not a gateway, not a channel.** Terms are "a payment method the account
enables alongside dealer pricing, never either-or" (Dana, 2026-09-02), and
never a Saleor channel (Dana, 2026-09-01). So there is no new payment app to
install, no plugin configuration, and no second channel whose price book has to
be kept in step: one boolean on the dealer record the merchant already keeps.

**Stock moves the money, or rather declines to.** An order placed now and
invoiced later is stock Saleor's own "charge later": a `TransactionItem` that
AUTHORIZES the full total and CHARGES nothing. `checkout.authorize_status`
becomes FULL, `complete_checkout` takes its transaction branch, and the order
lands `authorize_status=FULL, charge_status=NONE`. That is what an invoice IS,
and every stock screen, report, webhook and export that already understands an
authorized unpaid order understands this one for free.

`Channel.allow_unpaid_orders` was the other stock lever and was rejected. It is
per CHANNEL, so turning it on to serve one dealer lets EVERY shopper in that
channel place an unpaid order through stock `checkoutComplete`. Default deny is
the rule, so the permission lives on the buyer, and the only thing that can
create this authorization is the gated mutation in
`saleor/wsm/graphql/dealer/checkout.py`.

**Default deny, three ways.** No dealer record, no terms. A dealer record
without `invoice_payment`, no terms. `po_required` off until a merchant turns it
on, so the field is only demanded where it was asked for.

**Where the answer is written.** On the ORDER's metadata, not on a graphene
extension of stock's `Order` type. The fork extends a core graphene type once
already (MP4 on `Product`) and every such extension is a ledger entry in
`saleor/wsm/patches.py` plus a pinned core class; metadata is stock, costs no
patch, and rides the order query the storefront already makes. Public keys are
the shopper's copy (their own PO on their own order); the private keys are the
merchant's, because stock lets nobody but MANAGE_ORDERS write EITHER on an
order (`saleor/graphql/meta/permissions.py:333`) but a shopper CAN write public
metadata on their own CHECKOUT, and the checkout's metadata is copied onto the
order at creation. So the badge a merchant acts on is read from the private
copy, which no shopper can reach on either object.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings

from .models import ACCOUNT_STATUS_HOLD, DealerCustomer

# What the order carries, on both sides of the metadata fence. The public three
# are the shopper's receipt; the private two are what a merchant screen trusts.
TERMS_KEY = "wsm_terms"
PO_KEY = "po_number"
ACCOUNT_NUMBER_KEY = "wsm_account_number"
PRIVATE_TERMS_KEY = "wsm.terms"
PRIVATE_PO_KEY = "wsm.terms.po_number"

# The value `wsm_terms` carries. A string rather than "true" so a second terms
# arrangement (net 30, credit card on file) is a new value and not a new key.
TERMS_ON_ACCOUNT = "account"

# What the merchant sees in the payment card of the order. Stock renders
# `TransactionItem.name` there, so this is the sentence, not a code.
TRANSACTION_NAME = "Pay on account"

# The sentence a held account reads. One string, because the plugin that
# refuses the card path and the mutation that refuses the terms path are
# telling one shopper the same thing.
HOLD_MESSAGE = (
    "This account is on hold and cannot place orders. "
    "Please contact us to bring it back into good standing."
)


def account_for(user_pk, *, database_connection_name=None) -> DealerCustomer | None:
    """The dealer record behind this shopper, or None. ONE indexed read, from the id.

    Mirrors `pricing.tier_group_for`: the callers that need the record never
    need the `User` row, and `DealerCustomer.user` is a OneToOne, so there is
    exactly one answer and no join to a fetched object. Costs nothing at all for
    a shopper who is not signed in, because there is no id to ask about.
    """
    if not user_pk:
        return None
    db = database_connection_name or settings.DATABASE_CONNECTION_DEFAULT_NAME
    return DealerCustomer.objects.using(db).filter(user_id=user_pk).first()


def is_on_hold(user_pk, *, database_connection_name=None) -> bool:
    """Whether this shopper's account is on hold, so no order may be created.

    Hold is not a terms rule. 5.0 blocks checkout outright for 64 tenants and
    $308M TTM, card included, which is why the enforcement seam is the plugin in
    `saleor/wsm/dealer/plugin.py` and not this module's mutation: every path
    that turns a checkout into an order runs it.
    """
    account = account_for(user_pk, database_connection_name=database_connection_name)
    return account is not None and account.account_status == ACCOUNT_STATUS_HOLD


def order_stamps(account: DealerCustomer, po_number: str) -> tuple[dict, dict]:
    """The public and private metadata an order on account carries.

    Built here rather than at the call site so the storefront's receipt, the
    Dashboard's payment card and the test that reads them back all name the same
    five keys from one place.
    """
    public = {TERMS_KEY: TERMS_ON_ACCOUNT}
    private = {PRIVATE_TERMS_KEY: TERMS_ON_ACCOUNT}
    if po_number:
        public[PO_KEY] = po_number
        private[PRIVATE_PO_KEY] = po_number
    if account.account_number:
        public[ACCOUNT_NUMBER_KEY] = account.account_number
    return public, private


def authorize_on_terms(checkout, user, total: Decimal, manager, po_number: str):
    """Stock's "charge later": authorize the whole total, charge nothing.

    Returns the `TransactionItem`. Built exactly the way stock's own
    `transactionCreate` builds one (`create_transaction` there), because the
    amounts on a transaction are not columns a caller sets: they are RECOMPUTED
    from its events, so a row written with `authorized_value=` directly and no
    event behind it reads as authorized and reconciles to nothing.

    No `available_actions`. `CANCEL` and `CHARGE` are requests stock sends to
    the app that owns the transaction, and this one has no app: the merchant
    voids an invoice order by cancelling the ORDER, which is the screen they
    already use. Ceiling: a Dashboard "charge this now" button would need an app
    identifier here first.
    """
    from ...payment.models import TransactionItem
    from ...payment.transaction_item_calculations import recalculate_transaction_amounts
    from ...payment.utils import create_manual_adjustment_events

    transaction_item = TransactionItem.objects.create(
        token=uuid.uuid4(),
        use_old_id=True,
        checkout_id=checkout.pk,
        currency=checkout.currency,
        name=TRANSACTION_NAME,
        message=f"PO {po_number}" if po_number else "",
        user=user,
        available_actions=[],
    )
    create_manual_adjustment_events(
        transaction=transaction_item,
        money_data={"authorized_value": total},
        user=user,
        app=None,
    )
    recalculate_transaction_amounts(transaction_item)
    return transaction_item
