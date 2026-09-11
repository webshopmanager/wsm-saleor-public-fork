# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""`wsmCheckoutCompleteOnTerms`: place the order, invoice it later.

The one public mutation this layer has. Everything else under
`saleor/wsm/graphql/` is a merchant screen behind MANAGE_DISCOUNTS; this is a
shopper pressing "Place order", so the gate is not a permission, it is five
questions asked in order and a refusal code for each.

    1. signed in at all                      NOT_CHECKOUT_OWNER
    2. this checkout is theirs               NOT_CHECKOUT_OWNER
    3. a dealer record with terms enabled    TERMS_NOT_ENABLED
    4. the account is not on hold            ACCOUNT_ON_HOLD
    5. a PO number when the merchant asks    PO_REQUIRED

Question 2 covers the anonymous checkout as well as another shopper's: a
checkout with no user on it belongs to nobody, so it is not theirs either, and
one comparison answers both.

**The money is never taken from the cart.** The amount authorized is the total
`fetch_checkout_data` returns AFTER it has run, and running it is what re-derives
every price this fork owns (MP3, `saleor/wsm/reprice.py`): dealer tier prices,
composed option deltas and kit member prices are all recomputed from our tables
and the current line quantities before a number is authorized. The order is then
created by stock `complete_checkout`, so MP1 and MP2 (no discount stacks on a
dealer line), the compliance plugin (no part ships where it may not go) and the
hold plugin all run exactly as they do on a card order. Nothing here reimplements
a check, and nothing here skips one.

**The failure path matters more than the happy one.** An authorization is
written BEFORE completion, because that is the state `complete_checkout` refuses
without. If completion then fails, on a shipping restriction, an out-of-stock
line or a held account, the checkout would keep an authorization for an order
that does not exist and would look paid for. So the transaction is removed and
the checkout's payment statuses recomputed before the refusal is re-raised.
"""

from decimal import Decimal

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from ....checkout.calculations import fetch_checkout_data
from ....checkout.complete_checkout import complete_checkout
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.payment_utils import update_checkout_payment_statuses
from ....checkout.utils import get_or_create_checkout_metadata
from ....graphql.checkout.mutations.utils import get_checkout
from ....graphql.core import ResolveInfo
from ....graphql.core.context import SyncWebhookControlContext
from ....graphql.core.mutations import BaseMutation
from ....graphql.order.types import Order
from ....graphql.plugins.dataloaders import get_plugin_manager_promise
from ....graphql.site.dataloaders import get_site_promise
from ...dealer import models, terms
from ..errors import WsmErrorCode, WsmMutationMeta
from ..utils import error

# What a merchant may type into the PO box and what gets stored. Long enough for
# every 5.0 PO string seen in the account sweep, short enough that the column it
# lands in (order metadata, a JSON value) is not a place to paste a document.
MAX_PO_LENGTH = 100

# Every code `WsmError.code` can carry. A refusal raised inside stock carries a
# stock code, and graphene does not degrade one this enum does not have: it
# fails the whole RESPONSE. See `_sayable`.
KNOWN_CODES = frozenset(member.value for member in WsmErrorCode)


class WsmCheckoutCompleteOnTerms(WsmMutationMeta, BaseMutation):
    order = graphene.Field(
        Order, description="The order placed, unpaid and waiting on its invoice."
    )

    class Arguments:
        checkout_id = graphene.ID(
            required=True, description="The checkout to place as an order."
        )
        po_number = graphene.String(
            description=(
                "The buyer's purchase order number, stored on the order and shown "
                "to both sides. Required only when Dealer settings say so."
            )
        )

    class Meta:
        description = (
            "Place a checkout as an order to be invoiced, with no payment taken. "
            "Available only to a signed-in shopper whose dealer account the "
            "merchant has enabled for payment on account."
        )

    @classmethod
    def _buyer(cls, info: ResolveInfo):
        user = info.context.user
        # `not user`, never `user is None`: an anonymous request carries a
        # SimpleLazyObject WRAPPING None (see the same note in schema.py).
        if not user or not user.is_authenticated:
            raise ValidationError(
                {
                    "checkout_id": error(
                        "Sign in to place an order on account.",
                        "not_checkout_owner",
                    )
                }
            )
        return user

    @classmethod
    def _checkout_of(cls, info: ResolveInfo, checkout_id, user):
        checkout = get_checkout(cls, info, id=checkout_id)
        if checkout.user_id != user.pk:
            # Includes the anonymous checkout, whose `user_id` is None: a cart
            # that belongs to nobody does not belong to this buyer either.
            raise ValidationError(
                {
                    "checkout_id": error(
                        "This cart is not yours to place.",
                        "not_checkout_owner",
                    )
                }
            )
        return checkout

    @classmethod
    def _account_on_terms(cls, user) -> models.DealerCustomer:
        account = terms.account_for(user.pk)
        if account is None or not account.invoice_payment:
            raise ValidationError(
                {
                    "checkout_id": error(
                        "This account is not set up to pay on invoice.",
                        "terms_not_enabled",
                    )
                }
            )
        if account.account_status == models.ACCOUNT_STATUS_HOLD:
            raise ValidationError(
                {"checkout_id": error(terms.HOLD_MESSAGE, "account_on_hold")}
            )
        return account

    @classmethod
    def _po_number(cls, po_number) -> str:
        po = (po_number or "").strip()
        if len(po) > MAX_PO_LENGTH:
            raise ValidationError(
                {
                    "po_number": error(
                        f"a purchase order number is at most {MAX_PO_LENGTH} "
                        "characters",
                        "invalid",
                    )
                }
            )
        if not po and models.DealerSettings.po_required_for_store():
            raise ValidationError(
                {
                    "po_number": error(
                        "give the purchase order number for this order",
                        "po_required",
                    )
                }
            )
        return po

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, checkout_id, po_number=None
    ):
        user = cls._buyer(info)
        checkout = cls._checkout_of(info, checkout_id, user)
        account = cls._account_on_terms(user)
        po = cls._po_number(po_number)

        manager = get_plugin_manager_promise(info.context).get()
        lines, _unavailable = fetch_checkout_lines(checkout)
        checkout_info = fetch_checkout_info(checkout, lines, manager)

        # The number that gets authorized is the one this call produces, never
        # one the caller sent: MP3 re-derives every fork-owned price inside it.
        fetch_checkout_data(checkout_info, manager, lines, requestor=user).get()
        total = Decimal(checkout_info.checkout.total.gross.amount)

        public, private = terms.order_stamps(account, po)
        metadata = get_or_create_checkout_metadata(checkout_info.checkout)
        metadata.store_value_in_metadata(public)
        metadata.store_value_in_private_metadata(private)
        metadata.save(update_fields=["metadata", "private_metadata"])

        transaction_item = None
        if total > 0:
            # A zero-total checkout needs no authorization: stock completes it
            # on its own transaction branch (`checkout_is_zero`).
            transaction_item = terms.authorize_on_terms(
                checkout_info.checkout, user, total, manager, po
            )
            fetch_checkout_data(
                checkout_info,
                manager,
                lines,
                requestor=user,
                force_status_update=True,
            ).get()

        site = get_site_promise(info.context).get()
        try:
            order, _confirmation_needed, _confirmation_data = complete_checkout(
                manager=manager,
                checkout_info=checkout_info,
                lines=lines,
                payment_data={},
                store_source=False,
                user=user,
                app=None,
                site_settings=site.settings,
            )
        except ValidationError as refused:
            cls._release(transaction_item, checkout_info, lines)
            raise cls._sayable(refused) from refused
        except Exception:
            cls._release(transaction_item, checkout_info, lines)
            raise
        return cls(order=SyncWebhookControlContext(order) if order else None)

    @classmethod
    def _sayable(cls, refused: ValidationError) -> ValidationError:
        """Stock's refusal, in a code this mutation's error type can carry.

        `complete_checkout` raises stock `CheckoutErrorCode`s:
        `shipping_method_not_set`, `insufficient_stock`,
        `voucher_not_applicable`, and every refusal the compliance and hold
        plugins raise. None of them is a `WsmErrorCode`, and graphene does not
        degrade a value its enum does not have, it fails the whole RESPONSE
        with a protocol error. Measured 2026-09-11: a terms checkout with no
        delivery method chosen came back as a top-level GraphQL error, so the
        storefront got no field, no message and nothing to render.

        The MESSAGE is what a shopper reads and it is kept exactly as stock
        wrote it; only the code moves, and only when it is one this enum cannot
        say. Mapping rather than enumerating stock's codes is deliberate: an
        upstream release that adds a refusal reason still reaches the shopper as
        a sentence. ponytail: the ceiling is a storefront that wants to BRANCH
        on one of those reasons, which needs that code added to `WsmErrorCode`
        by name; the upgrade is one enum member and nothing here.
        """

        def repack(single: ValidationError) -> ValidationError:
            code = getattr(single, "code", None)
            return ValidationError(
                single.message,
                code=code if code in KNOWN_CODES else "invalid",
                params=getattr(single, "params", None),
            )

        if hasattr(refused, "error_dict"):
            return ValidationError(
                {
                    field: [repack(single) for single in errors]
                    for field, errors in refused.error_dict.items()
                }
            )
        return ValidationError([repack(single) for single in refused.error_list])

    @classmethod
    def _release(cls, transaction_item, checkout_info, lines):
        """Undo the authorization when the order was refused after it was made.

        Without this a refused terms checkout keeps a full authorization: the
        cart reads as paid for, stock's own automatic-completion path would see
        an authorized checkout and try again, and the merchant's unpaid-order
        report gains a row with no order under it.
        """
        if transaction_item is None:
            return
        with db_transaction.atomic():
            transaction_item.delete()
            checkout = checkout_info.checkout
            update_checkout_payment_statuses(
                checkout=checkout,
                checkout_total_gross=checkout.total.gross,
                checkout_has_lines=bool(lines),
            )
