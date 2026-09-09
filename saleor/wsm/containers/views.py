# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one endpoint a kit needs: put the members in the checkout, priced by us.

There is no GraphQL in this unit and no new field on Collection. A series
collection page is the STOCK collection page; everything the storefront needs to
draw the configurator is on the Collection's own metadata under `wsm.series`
(see models.SeriesConfig.stamp_collection), which Saleor already returns.
"""

import base64
import binascii
import json
from decimal import Decimal

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ...account.models import User
from ...checkout.models import Checkout
from ...core.db.connection import allow_writer
from ..checkout import LineRefused, whole_number
from ..dealer import pricing as dealer_pricing
from ..dealer.tax import bind_tax_exemption
from ..http import storefront_key_required
from . import pricing
from .models import KitConfig


def _money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _from_gid(raw: str, expected: str) -> str | None:
    """Return the primary key inside a Saleor global id, or None if the type is wrong.

    Padding is restored before decoding: a GID that lost its `=` in a URL is a
    routine thing to receive and refusing it would be a false 404.
    """
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    type_name, sep, pk = decoded.partition(":")
    if not sep or type_name != expected or not pk:
        return None
    return pk


def _to_gid(type_name: str, pk) -> str:
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


def _not_found(what: str):
    return JsonResponse({"violations": [f"unknown {what}"]}, status=404)


def _refused(message: str):
    return JsonResponse({"violations": [message]}, status=422)


def resolve_tier_lookup(kit, checkout, user, group_code=None):
    """Return the dealer tier lookup for this buyer, or None for plain retail.

    The seam, in one place. `pricing.price_kit` already takes the better of a
    tier and the kit-discounted unit, so wiring wsm.dealer in is handing it the
    callable that app already exposes: nothing about the kit money changes here.

    Every break for every member is read in ONE query, up front, rather than one
    query per member from inside the pricing loop: `ladders` is built to answer
    a page of variants at a time and a kit is smaller than a page. A shopper who
    is not signed in costs nothing at all, because there is no query to make.

    The writer connection is passed for the reason the dealer endpoints pass it:
    a price about to be stamped on a checkout line is read from the database the
    line is written to, not from a replica that may lag behind the merchant.
    """
    if user is None and not group_code:
        return None

    # `group_code` is the recalculation's way in. The add resolves the customer
    # server side and never attaches them to the checkout, so by the time MP3
    # re-derives these lines there is no user to ask and the group stamped on
    # the line at add time is the authority. Private metadata, so it is ours to
    # trust. See reprice.py.
    breaks = dealer_pricing.ladders(
        user,
        checkout.channel,
        list(kit.members.values_list("variant_id", flat=True)),
        group_codes=[group_code] if user is None else None,
        database_connection_name=settings.DATABASE_CONNECTION_DEFAULT_NAME,
    )
    if not breaks:
        return None

    def tier_lookup(variant, _user, quantity):
        winner = dealer_pricing.best_break(breaks.get(variant.pk, []), quantity)
        return winner.amount if winner is not None else None

    return tier_lookup


@csrf_exempt
@require_POST
@storefront_key_required
# Saleor routes reads to a replica and refuses the writer unless a view asks for
# it, exactly as its own webhook views do (saleor/plugins/views.py). This view
# writes checkout lines, so it asks.
@allow_writer()
def kit_line(request):
    """Explode one kit into its member lines at server-computed prices."""
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _refused("body is not JSON")

    checkout_token = _from_gid(body.get("checkoutId", ""), "Checkout")
    collection_pk = _from_gid(body.get("collectionId", ""), "Collection")
    if checkout_token is None:
        return _not_found("checkout")
    if collection_pk is None or not collection_pk.isdigit():
        return _not_found("kit")

    quantity = whole_number(body.get("quantity") or 1)
    if quantity is None:
        return _refused("quantity must be a whole number")
    if quantity < 1:
        return _refused("quantity must be at least 1")

    checkout = (
        Checkout.objects.select_related("channel").filter(token=checkout_token).first()
    )
    if checkout is None:
        return _not_found("checkout")

    kit = (
        KitConfig.objects.select_related("collection")
        .filter(collection_id=collection_pk)
        .first()
    )
    if kit is None:
        return _not_found("kit")
    if not kit.active:
        return _refused("this kit is not active")

    user = None
    raw_customer = body.get("customerId")
    if raw_customer:
        customer_pk = _from_gid(raw_customer, "User")
        if customer_pk is None:
            return _not_found("customer")
        user = User.objects.filter(pk=customer_pk).first()
        if user is None:
            return _not_found("customer")

    tier_lookup = resolve_tier_lookup(kit, checkout, user)
    # Only a buyer who can actually reach a tier costs the group lookup: with no
    # ladder on any member there is no dealer line to stamp. A lookup without a
    # user is a caller supplying its own, and it gets no group rather than a
    # crash on the way to one.
    tier_group = (
        dealer_pricing.tier_group_for(
            user.pk, database_connection_name=settings.DATABASE_CONNECTION_DEFAULT_NAME
        )
        if tier_lookup is not None and user is not None
        else None
    )

    try:
        group_id, priced, lines_by_variant = pricing.add_kit_to_checkout(
            checkout,
            kit,
            quantity,
            user=user,
            tier_lookup=tier_lookup,
            tier_group=tier_group,
        )
    except pricing.KitRefusal as refusal:
        return _refused(str(refusal))
    except LineRefused as refusal:
        return JsonResponse({"violations": refusal.violations}, status=422)

    # Same reason as the configured line: a kit is what this buyer is buying,
    # so it is where their exemption has to land.
    bind_tax_exemption(
        checkout,
        user.pk if user is not None else None,
        database_connection_name=settings.DATABASE_CONNECTION_DEFAULT_NAME,
    )

    return JsonResponse(
        {
            "checkoutId": body.get("checkoutId"),
            "groupId": group_id,
            "lines": [
                {
                    "lineId": (
                        _to_gid("CheckoutLine", line.pk)
                        if (line := lines_by_variant.get(priced_line.member.variant.pk))
                        else None
                    ),
                    "variantId": _to_gid(
                        "ProductVariant", priced_line.member.variant.pk
                    ),
                    "unitPrice": _money(priced_line.unit_cents),
                    "share": priced_line.share,
                }
                for priced_line in priced.lines
            ],
            "kitTotal": _money(priced.total_cents),
        }
    )
