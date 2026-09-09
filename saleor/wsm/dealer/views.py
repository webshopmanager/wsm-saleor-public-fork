# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The three storefront endpoints, mounted at /wsm/dealer_pricing/.

Plain Django views: the payloads are three small JSON objects and `json` already
does the work a serializer layer would. The contract is the one today's
storefront already speaks, so B6 holds with no storefront code (design doc
section 2, "Stays").

The caller is the storefront SERVER, and it proves it with the tenant's
`X-Dealer-Pricing-Key` (saleor/wsm/http.py). What a caller could forge was never
a price, which is why "nothing to forge" read true: it is the BUYER. `customerId`
arrives in the body, so without the key any stranger could read what a dealer
pays and add lines to a checkout at that dealer's tier. No caller-supplied price
is ever honoured (requirement 1.4) and none is ever read; `X-Saleor-Domain` still
arrives and is still ignored, because one process serves one tenant.

Not being a dealer is never an error. It is an empty ladder and a retail line,
because the same storefront code runs for every shopper and a 4xx on the common
case would put error handling in front of the majority of visitors.
"""

from __future__ import annotations

import json

import graphene
from django.conf import settings
from django.contrib.sites.models import Site
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ...account.models import User
from ...channel.models import Channel
from ...checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ...checkout.models import Checkout, CheckoutLine
from ...checkout.utils import add_variants_to_checkout, invalidate_checkout
from ...core.db.connection import allow_writer
from ...core.prices import quantize_price
from ...graphql.checkout.mutations.utils import CheckoutLineData
from ...plugins.manager import get_plugins_manager
from ...product.models import ProductVariant, ProductVariantChannelListing
from ..checkout import LineRefused, check_addable, whole_number
from ..http import storefront_key_required
from . import pricing
from .no_stacking import LINE_METADATA_KEY, PRICE_OVERRIDE_REASON
from .tax import bind_tax_exemption


# Saleor routes every read it can to the replica and guards the writer, so a
# view that reads the writer without saying so raises (`UnsafeWriterAccessError`,
# saleor/core/db/connection.py). Endpoint 3 only ever reads, so it takes the
# replica. Endpoints 4 and 5 write a checkout line and read the rows they are
# about to write, so they declare the writer once, at the view.
REPLICA = settings.DATABASE_CONNECTION_REPLICA_NAME
WRITER = settings.DATABASE_CONNECTION_DEFAULT_NAME


def _body(request) -> dict:
    try:
        parsed = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pk(global_id, expected_type: str):
    """The database id inside a Saleor GID, or None. Raw ids are accepted too.

    The storefront sends GIDs; the admin and curl send whatever is at hand, and
    refusing a raw primary key would buy nothing.
    """
    if not global_id:
        return None
    try:
        type_name, raw = graphene.Node.from_global_id(str(global_id))
    except Exception:
        return str(global_id)
    if type_name != expected_type:
        return None
    return raw


def _customer(customer_id, db):
    pk = _pk(customer_id, "User")
    if not pk:
        return None
    return User.objects.using(db).filter(pk=pk).first()


def _error(code: str, status: int):
    return JsonResponse({"error": code}, status=status)


def _retail_amount(variant_id, channel, db):
    listing = (
        ProductVariantChannelListing.objects.using(db)
        .filter(variant_id=variant_id, channel_id=channel.pk)
        .first()
    )
    return None if listing is None else listing.price_amount


def _money(amount, channel):
    if amount is None:
        return None
    return str(quantize_price(amount, channel.currency_code))


def _invalidate(checkout):
    """The tail every write path owes the checkout, the one stock code runs.

    `price_expiration` is what tells the next read to recompute, so a line
    written outside a mutation and left un-stamped is a cart that keeps quoting
    the price it held a moment ago. It also runs the discount recalculation,
    which is where a promotion that must come off a line that just became a
    dealer line comes off.
    """
    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info.lines = lines
    invalidate_checkout(checkout_info, lines, manager, save=True)


@csrf_exempt
@require_POST
@storefront_key_required
def storefront_prices(request):
    """Endpoint 3. Every break the buyer can reach on up to 100 variants."""
    body = _body(request)
    variant_ids = body.get("variantIds") or []
    if not isinstance(variant_ids, list):
        return _error("variantIdsMustBeAList", 400)
    if len(variant_ids) > pricing.MAX_BATCH:
        return _error("tooManyVariants", 413)

    channel = Channel.objects.using(REPLICA).filter(slug=body.get("channel")).first()
    user = _customer(body.get("customerId"), REPLICA)
    if channel is None or user is None:
        return JsonResponse({"breaks": {}})

    pks = [pk for pk in (_pk(gid, "ProductVariant") for gid in variant_ids) if pk]
    breaks = pricing.prices_for_variants(
        user, channel, pks, database_connection_name=REPLICA
    )
    return JsonResponse({"breaks": breaks})


@csrf_exempt
@require_POST
@storefront_key_required
@allow_writer()
def dealer_line(request):
    """Endpoint 4. Add one line, priced at the buyer's break where there is one."""
    body = _body(request)

    checkout = Checkout.objects.filter(pk=_pk(body.get("checkoutId"), "Checkout")).first()
    if checkout is None:
        return _error("checkoutNotFound", 404)
    channel = Channel.objects.filter(slug=body.get("channel")).first()
    if channel is None or checkout.channel_id != channel.pk:
        return _error("channelMismatch", 409)

    variant = ProductVariant.objects.filter(
        pk=_pk(body.get("variantId"), "ProductVariant")
    ).first()
    if variant is None:
        return _error("variantNotFound", 404)

    quantity = whole_number(body.get("quantity", 1))
    if quantity is None:
        return _error("quantityMustBeAWholeNumber", 422)
    if quantity < 1:
        return _error("invalidQuantity", 400)

    user = _customer(body.get("customerId"), WRITER)
    breaks = pricing.ladders(
        user, channel, [variant.pk], database_connection_name=WRITER
    ).get(variant.pk, [])
    winner = pricing.best_break(breaks, quantity)
    # A buyer with a ladder who has not reached its bottom rung is told so, and
    # not quietly sold the same thing at retail: the storefront has a quantity
    # box to nudge. A buyer with no ladder at all is not a dealer here and takes
    # the retail line without a word.
    if breaks and winner is None:
        return _error("belowBreak", 422)

    try:
        line = _add_line(checkout, channel, variant, quantity, winner, user)
    except LineRefused as refusal:
        return JsonResponse(
            {"error": "lineRefused", "violations": refusal.violations}, status=422
        )
    return JsonResponse(
        {
            "lineId": graphene.Node.to_global_id("CheckoutLine", line.pk),
            "unitPrice": _money(
                line.price_override
                if line.price_override is not None
                else _retail_amount(variant.pk, channel, WRITER),
                channel,
            ),
            "dealerPrice": _money(winner.amount, channel) if winner else None,
            "minQuantity": winner.min_quantity if winner else None,
        }
    )


def _add_line(checkout, channel, variant, quantity, winner, user):
    """Create the line through the stock add-lines path, then hand it back.

    `add_variants_to_checkout` returns the checkout rather than the line, and it
    creates a NEW line whenever no line id is given, so the new line is the one
    id that was not there a moment ago. Reading the pks before and after is one
    small query and beats guessing with `.last()` on a checkout that may already
    hold the same variant at retail.
    """
    # PRIVATE metadata, because this stamp is a pricing input: MP3 reads the
    # group off it to re-derive an anonymous checkout's ladder, and stock Saleor
    # lets any unauthenticated caller write PUBLIC line metadata
    # (saleor/graphql/meta/permissions.py maps CheckoutLine to `no_permissions`).
    # It is written after the add rather than through `metadata_list`, which
    # `add_variants_to_checkout` only ever stores publicly.
    stamp = (
        {
            LINE_METADATA_KEY: json.dumps(
                {"group": winner.group_code, "minQuantity": winner.min_quantity}
            )
        }
        if winner
        else {}
    )

    line_data = CheckoutLineData(
        variant_id=str(variant.pk),
        quantity=quantity,
        quantity_to_update=True,
        custom_price=winner.amount if winner else None,
        custom_price_to_update=bool(winner),
        custom_price_reason=PRICE_OVERRIDE_REASON if winner else None,
        custom_price_reason_to_update=bool(winner),
    )
    # The checks `checkoutLinesAdd` runs before the identical write. Raises
    # `LineRefused`, which the view turns into a 422.
    check_addable(
        checkout,
        channel,
        [variant],
        [line_data],
        site_settings=Site.objects.get_current().settings,
    )

    with transaction.atomic():
        before = set(
            CheckoutLine.objects.filter(checkout_id=checkout.pk).values_list(
                "pk", flat=True
            )
        )
        add_variants_to_checkout(
            checkout,
            [variant],
            [line_data],
            channel,
            calculate_stocks_with_shipping_zones=False,
        )
        line = (
            CheckoutLine.objects.filter(checkout_id=checkout.pk)
            .exclude(pk__in=before)
            .first()
        )
        if line is not None and stamp:
            line.store_value_in_private_metadata(stamp)
            line.save(update_fields=["private_metadata"])
        # The buyer's account facts land in the same transaction as the line
        # they were read for, so an add that is rolled back leaves neither
        # behind. `bind_tax_exemption` expires the prices itself when the flag
        # moves; the unconditional `_invalidate` below is for the line.
        bind_tax_exemption(
            checkout, getattr(user, "pk", None), database_connection_name=WRITER
        )
        _invalidate(checkout)
        return line


@csrf_exempt
@require_POST
@storefront_key_required
@allow_writer()
def dealer_line_reprice(request):
    """Endpoint 5. Re-run the ladder against the line's CURRENT quantity.

    Idempotent by construction: the answer is a function of the line and the
    catalog, never of what the line was priced at before, and the write is
    skipped when nothing moved.
    """
    body = _body(request)

    checkout = Checkout.objects.filter(pk=_pk(body.get("checkoutId"), "Checkout")).first()
    if checkout is None:
        return _error("checkoutNotFound", 404)
    channel = Channel.objects.filter(slug=body.get("channel")).first()
    if channel is None or checkout.channel_id != channel.pk:
        return _error("channelMismatch", 409)

    line = (
        CheckoutLine.objects.filter(
            checkout_id=checkout.pk, pk=_pk(body.get("lineId"), "CheckoutLine")
        )
        .select_related("variant")
        .first()
    )
    if line is None:
        return _error("lineNotFound", 404)
    # Repricing means deciding this line's price, and a price another app wrote
    # is not this app's to decide. Compose and the kit endpoint stamp their own
    # reason; clearing one here would sell a configured line at its bare base
    # price. A line with no override at all is nobody's and is fair game.
    if (
        line.price_override is not None
        and line.price_override_reason != PRICE_OVERRIDE_REASON
    ):
        return _error("foreignPriceOverride", 409)

    user = _customer(body.get("customerId"), WRITER)
    winner = pricing.dealer_price_for(
        line.variant_id,
        user,
        line.quantity,
        channel=channel,
        database_connection_name=WRITER,
    )

    def state():
        return (
            line.price_override,
            line.price_override_reason,
            dict(line.private_metadata or {}),
        )

    was = state()
    if winner:
        line.price_override = winner.amount
        line.price_override_reason = PRICE_OVERRIDE_REASON
        line.store_value_in_private_metadata(
            {
                LINE_METADATA_KEY: json.dumps(
                    {"group": winner.group_code, "minQuantity": winner.min_quantity}
                )
            }
        )
    else:
        line.price_override = None
        line.price_override_reason = None
        line.delete_value_from_private_metadata(LINE_METADATA_KEY)
    with transaction.atomic():
        if was != state():
            line.save(
                update_fields=[
                    "price_override",
                    "price_override_reason",
                    "private_metadata",
                ]
            )
            # Only when the line actually moved: a reprice that changed nothing
            # is a read, and expiring the prices on every poll would put the
            # whole checkout through a recalculation the storefront never asked
            # for.
            _invalidate(checkout)
        # Asked on every call, moved line or not: an exemption can be revoked
        # while a cart sits untouched, and this is the route the storefront
        # calls when it comes back. It expires the prices itself when it moves,
        # so a reprice that changed nothing still costs nothing.
        bind_tax_exemption(
            checkout, getattr(user, "pk", None), database_connection_name=WRITER
        )

    base = _retail_amount(line.variant_id, channel, WRITER)
    return JsonResponse(
        {
            "lineId": graphene.Node.to_global_id("CheckoutLine", line.pk),
            "unitPrice": _money(
                line.price_override if line.price_override is not None else base, channel
            ),
            "basePrice": _money(base, channel),
            "dealerPrice": _money(winner.amount, channel) if winner else None,
        }
    )
