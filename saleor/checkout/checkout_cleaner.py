import datetime
import json
import logging
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import graphene
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from ..core.exceptions import GiftCardNotApplicable
from ..core.taxes import TaxError
from ..giftcard.models import GiftCard
from ..payment import gateway
from ..payment import models as payment_models
from ..payment.error_codes import PaymentErrorCode
from ..plugins.manager import PluginsManager
from . import models
from .delivery_context import clear_cc_delivery_method, is_shipping_required
from .error_codes import CheckoutErrorCode, OrderCreateFromCheckoutErrorCode
from .models import Checkout
from .utils import is_fully_paid

if TYPE_CHECKING:
    from .fetch import CheckoutInfo, CheckoutLineInfo

logger = logging.getLogger(__name__)


def clean_checkout_shipping(
    checkout_info: "CheckoutInfo",
    lines: list["CheckoutLineInfo"],
    error_code: (
        type[CheckoutErrorCode]
        | type[PaymentErrorCode]
        | type[OrderCreateFromCheckoutErrorCode]
    ),
):
    delivery_method_info = checkout_info.get_delivery_method_info()

    if is_shipping_required(lines):
        if not delivery_method_info.delivery_method:
            raise ValidationError(
                {
                    "shipping_method": ValidationError(
                        "Shipping method is not set",
                        code=error_code.SHIPPING_METHOD_NOT_SET.value,
                    )
                }
            )
        if not delivery_method_info.is_valid_delivery_method():
            raise ValidationError(
                {
                    "shipping_address": ValidationError(
                        "Shipping address is not set",
                        code=error_code.SHIPPING_ADDRESS_NOT_SET.value,
                    )
                }
            )
        if not delivery_method_info.is_method_in_valid_methods(checkout_info):
            if checkout_info.checkout.collection_point_id:
                clear_cc_delivery_method(checkout_info)
            raise ValidationError(
                {
                    "shipping_method": ValidationError(
                        "Delivery method is not valid for your shipping address",
                        code=error_code.INVALID_SHIPPING_METHOD.value,
                    )
                }
            )


def clean_billing_address(
    checkout_info: "CheckoutInfo",
    error_code: (
        type[CheckoutErrorCode]
        | type[PaymentErrorCode]
        | type[OrderCreateFromCheckoutErrorCode]
    ),
):
    if not checkout_info.billing_address:
        raise ValidationError(
            {
                "billing_address": ValidationError(
                    "Billing address is not set",
                    code=error_code.BILLING_ADDRESS_NOT_SET.value,
                )
            }
        )


def clean_checkout_payment(
    manager: PluginsManager,
    checkout_info: "CheckoutInfo",
    lines: list["CheckoutLineInfo"],
    error_code: type[CheckoutErrorCode],
    last_payment: payment_models.Payment | None,
):
    clean_billing_address(checkout_info, error_code)
    if not is_fully_paid(manager, checkout_info, lines):
        gateway.payment_refund_or_void(
            last_payment, manager, channel_slug=checkout_info.channel.slug
        )
        raise ValidationError(
            "Provided payment methods can not cover the checkout's total amount",
            code=error_code.CHECKOUT_NOT_FULLY_PAID.value,
        )


def _build_checkout_payment_snapshot(checkout_info: "CheckoutInfo") -> dict:
    """Capture what the customer is actually paying for, right now.

    Taken at the one point every payment app's charge attempt passes through
    (`clean_checkout_ready_for_payment`, just below), so it's available
    regardless of which payment app is involved — no per-app implementation
    needed. This is the source of truth for what was paid for: the checkout
    itself is free to change after this (add/remove lines, swap shipping) —
    that's normal and allowed — but this snapshot doesn't change with it. If
    the amount actually charged and the checkout's contents disagree by the
    time payment finishes, whoever resolves that (our own reconciliation, or
    a human doing manual review) has a precise record of what the paid-for
    snapshot was, rather than just a mismatched total with no explanation.
    """
    checkout = checkout_info.checkout
    shipping_method = checkout_info.checkout.shipping_method
    return {
        "snapshotted_at": timezone.now().isoformat(),
        "checkout_id": graphene.Node.to_global_id("Checkout", checkout.pk),
        "currency": checkout.currency,
        "total_gross_amount": str(checkout.total_gross_amount),
        "total_net_amount": str(checkout.total_net_amount),
        "lines": [
            {
                "variant_id": graphene.Node.to_global_id(
                    "ProductVariant", line.variant.pk
                ),
                "variant_sku": line.variant.sku,
                "product_name": line.product.name,
                "variant_name": line.variant.name,
                "quantity": line.line.quantity,
            }
            for line in checkout_info.lines
        ],
        "shipping_method_name": shipping_method.name if shipping_method else None,
        "shipping_price_gross_amount": (
            str(checkout.shipping_price_gross_amount)
            if checkout.is_shipping_required()
            else None
        ),
    }


def get_checkout_payment_snapshot(checkout: Checkout) -> dict | None:
    """Read back the "what was actually paid for" snapshot recorded earlier.

    Recorded by `clean_checkout_ready_for_payment` at charge time, from
    whichever transaction on this checkout actually has money on it
    (authorized or charged). A checkout can carry other, unrelated $0
    transaction attempts (e.g. one that lost a concurrency lock) whose
    snapshot, if any, isn't the one that matters here.

    Prefers the snapshot taken at TransactionProcess time over the one
    taken at TransactionInitialize: for a two-step (authorize now, capture
    later) flow, TransactionProcess is when the money actually moves and is
    the more relevant reference point; for a one-step (auth+capture
    together) flow only the TransactionInitialize snapshot will exist.

    Returns None if no such transaction/snapshot exists — this makes the
    check a no-op for transactions created before this feature existed, and
    for any flow that never goes through TransactionInitialize/
    TransactionProcess at all (e.g. a manually-created order).
    """
    paid_transactions = checkout.payment_transactions.filter(
        Q(authorized_value__gt=0) | Q(charged_value__gt=0)
    ).order_by("created_at")

    for transaction_item in paid_transactions:
        raw = transaction_item.private_metadata.get(
            "checkout_payment_snapshot_process"
        ) or transaction_item.private_metadata.get(
            "checkout_payment_snapshot_initialize"
        )
        if not raw:
            continue
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Could not parse checkout payment snapshot on transaction %s; "
                "skipping it.",
                transaction_item.pk,
            )
            continue

    return None


def _line_quantity_map(lines: list[dict]) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for line in lines:
        variant_id = line["variant_id"]
        quantities[variant_id] = quantities.get(variant_id, 0) + line["quantity"]
    return quantities


def _describe_line(lines: list[dict], variant_id: str) -> str:
    for line in lines:
        if line["variant_id"] == variant_id:
            return line.get("variant_sku") or line["product_name"]
    return variant_id


def _decimal(value: str | None) -> Decimal:
    if not value:
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(0)


_CENTS_TOLERANCE = Decimal("0.01")


def diff_checkout_payment_snapshot(paid_for: dict, current: dict) -> dict | None:
    """Compare what was actually paid for against the checkout's current state.

    Returns None when nothing meaningfully changed.

    `content_changed` is True only when the actual line items (variant/
    quantity) changed — the one signal precise enough to safely block order
    creation on. Shipping/tax drift alone is common and often benign (a
    shipping method swap, a tax recalculation, a voucher applied later) and
    is reported but doesn't block — it's surfaced for a human to see, not
    treated as an error.

    Ported from (and now authoritative over) the equivalent TypeScript
    implementations in the payment apps' `checkout-snapshot.ts` — this is
    the single enforcement point for every completion path
    (`create_order_from_checkout`), not just app-driven reconciliation.
    """
    before = _line_quantity_map(paid_for["lines"])
    after = _line_quantity_map(current["lines"])
    all_variant_ids = set(before) | set(after)

    changes: list[str] = []
    content_changed = False

    for variant_id in all_variant_ids:
        before_qty = before.get(variant_id, 0)
        after_qty = after.get(variant_id, 0)
        if before_qty == after_qty:
            continue

        content_changed = True
        label = _describe_line(paid_for["lines"], variant_id)
        if label == variant_id:
            label = _describe_line(current["lines"], variant_id)

        if after_qty == 0:
            changes.append(f"removed {before_qty}x {label}")
        elif before_qty == 0:
            changes.append(f"added {after_qty}x {label}")
        else:
            changes.append(f"{label}: {before_qty}x -> {after_qty}x")

    paid_shipping = _decimal(paid_for.get("shipping_price_gross_amount"))
    current_shipping = _decimal(current.get("shipping_price_gross_amount"))
    if abs(paid_shipping - current_shipping) > _CENTS_TOLERANCE:
        before_label = paid_for.get("shipping_method_name") or "no shipping method"
        after_label = current.get("shipping_method_name") or "no shipping method"
        changes.append(
            f"shipping changed: {before_label} (${paid_shipping}) -> "
            f"{after_label} (${current_shipping})"
        )
    elif paid_for.get("shipping_method_name") != current.get("shipping_method_name"):
        changes.append(
            "shipping method changed: "
            f"{paid_for.get('shipping_method_name') or 'none'} -> "
            f"{current.get('shipping_method_name') or 'none'}"
        )

    paid_tax = _decimal(paid_for.get("total_gross_amount")) - _decimal(
        paid_for.get("total_net_amount")
    )
    current_tax = _decimal(current.get("total_gross_amount")) - _decimal(
        current.get("total_net_amount")
    )
    if abs(paid_tax - current_tax) > _CENTS_TOLERANCE:
        changes.append(f"tax changed: ${paid_tax} -> ${current_tax}")

    if not changes:
        return None

    return {
        "content_changed": content_changed,
        "message": (
            "Checkout differs from what was actually paid for (charged "
            f"against total ${paid_for.get('total_gross_amount')}, checkout "
            f"is now ${current.get('total_gross_amount')}): "
            f"{'; '.join(changes)}."
        ),
    }


def clean_checkout_ready_for_payment(
    checkout: Checkout,
    manager: PluginsManager,
    error_code: str,
) -> dict:
    """Validate a checkout is complete enough to accept a payment attempt.

    Called from `TransactionInitialize`/`TransactionProcess` before dispatching
    `TRANSACTION_INITIALIZE_SESSION`/`TRANSACTION_PROCESS_SESSION` to a payment
    app — without this, a payment app can charge real money against a checkout
    that can never actually complete (e.g. no shipping method set), leaving a
    real charge with no order and no way to recover automatically. Reuses the
    same checks `checkoutComplete` itself performs (`validate_checkout` in this
    module), just run earlier, before any money moves — a checkout that would
    fail here would also fail at `checkoutComplete` today, just after a
    payment app already charged it.

    Returns a JSON-serializable snapshot of the checkout's contents at this
    exact moment — see `_build_checkout_payment_snapshot` — for the caller to
    attach to the transaction being created/processed.
    """
    from .fetch import fetch_checkout_info, fetch_checkout_lines

    lines, unavailable_variant_pks = fetch_checkout_lines(checkout)
    if unavailable_variant_pks or not lines:
        raise ValidationError(
            "Checkout is not ready for payment: it has no available lines.",
            code=error_code,
        )

    checkout_info = fetch_checkout_info(checkout, lines, manager)
    try:
        clean_billing_address(checkout_info, CheckoutErrorCode)
        clean_checkout_shipping(checkout_info, lines, CheckoutErrorCode)
    except ValidationError as e:
        # clean_billing_address/clean_checkout_shipping raise dict-style
        # ValidationErrors (no plain .message) — .messages flattens either
        # form into a list of strings.
        reason = "; ".join(e.messages)
        raise ValidationError(
            f"Checkout is not ready for payment: {reason}",
            code=error_code,
        ) from e

    # This snapshot is a diagnostic aid, not a payment precondition — a bug in
    # building it must never block a real charge from proceeding. Fail open
    # with an empty dict rather than letting an unexpected error here turn
    # into a 500 on the payment path.
    try:
        return _build_checkout_payment_snapshot(checkout_info)
    except Exception:
        logger.exception(
            "Failed to build checkout payment snapshot for checkout %s; "
            "continuing without it.",
            checkout.pk,
        )
        return {}


def validate_checkout_email(checkout: models.Checkout):
    if not checkout.email:
        raise ValidationError(
            "Checkout email must be set.",
            code=CheckoutErrorCode.EMAIL_NOT_SET.value,
        )


def _validate_gift_cards(checkout: Checkout):
    """Check if all gift cards assigned to checkout are available."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    all_gift_cards = GiftCard.objects.filter(checkouts=checkout.token).count()
    active_gift_cards = (
        GiftCard.objects.active(date=today).filter(checkouts=checkout.token).count()
    )
    if not all_gift_cards == active_gift_cards:
        msg = "Gift card has expired. Order placement cancelled."
        raise GiftCardNotApplicable(msg)

    # Re-check restricted gift cards at completion time: a card may have been
    # assigned to another customer after it was added to the checkout.
    # This also acts as a defense-in-depth safeguard in case an actor
    # may successfully add a giftcard that they are not authorized
    # to use into their checkout (shouldn't happen)
    # defense-in-depth safeguard, not a redundant check — do not remove it.
    #
    # We deliberately do not acquire a row lock here. A gift card changing hands
    # mid-checkoutComplete() is outside the threat model, and locking would have
    # a real performance/reliability cost for an unrealistic security risk.
    restricted = GiftCard.objects.filter(
        checkouts=checkout.token, assigned_to_email__isnull=False
    )
    if checkout.user_id:
        restricted = restricted.exclude(assigned_to_id=checkout.user_id)
    if restricted.exists():
        # Generic message — do not reveal the assignee.
        raise GiftCardNotApplicable(
            "Gift card cannot be used. Order placement cancelled."
        )


def validate_checkout(
    checkout_info: "CheckoutInfo",
    lines: list["CheckoutLineInfo"],
    unavailable_variant_pks: Iterable[int],
    manager: "PluginsManager",
):
    """Validate all required data for converting checkout into order."""
    if not checkout_info.channel.is_active:
        raise ValidationError(
            {
                "channel": ValidationError(
                    "Cannot complete checkout with inactive channel.",
                    code=OrderCreateFromCheckoutErrorCode.CHANNEL_INACTIVE.value,
                )
            }
        )
    if unavailable_variant_pks:
        not_available_variants_ids = {
            graphene.Node.to_global_id("ProductVariant", pk)
            for pk in unavailable_variant_pks
        }
        code = OrderCreateFromCheckoutErrorCode.UNAVAILABLE_VARIANT_IN_CHANNEL.value
        raise ValidationError(
            {
                "lines": ValidationError(
                    "Some of the checkout lines variants are unavailable.",
                    code=code,
                    params={"variants": not_available_variants_ids},
                )
            }
        )
    if not lines:
        raise ValidationError(
            {
                "lines": ValidationError(
                    "Cannot complete checkout without lines",
                    code=OrderCreateFromCheckoutErrorCode.NO_LINES.value,
                )
            }
        )

    if checkout_info.checkout.voucher_code and not checkout_info.voucher:
        raise ValidationError(
            {
                "voucher_code": ValidationError(
                    "Voucher not applicable",
                    code=OrderCreateFromCheckoutErrorCode.VOUCHER_NOT_APPLICABLE.value,
                )
            }
        )
    validate_checkout_email(checkout_info.checkout)

    clean_billing_address(checkout_info, OrderCreateFromCheckoutErrorCode)
    clean_checkout_shipping(checkout_info, lines, OrderCreateFromCheckoutErrorCode)
    _validate_gift_cards(checkout_info.checkout)

    # call plugin's hooks to validate if we are able to create an order
    # can raise TaxError
    try:
        manager.preprocess_order_creation(checkout_info, lines)
    except TaxError as e:
        raise ValidationError(
            f"Unable to calculate taxes - {str(e)}",
            code=OrderCreateFromCheckoutErrorCode.TAX_ERROR.value,
        ) from e
