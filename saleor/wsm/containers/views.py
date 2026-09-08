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
from ..dealer import pricing as dealer_pricing
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


def resolve_tier_lookup(kit, checkout, user):
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
    if user is None:
        return None

    breaks = dealer_pricing.ladders(
        user,
        checkout.channel,
        list(kit.members.values_list("variant_id", flat=True)),
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

    quantity = int(body.get("quantity") or 1)
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

    try:
        group_id, priced, lines_by_variant = pricing.add_kit_to_checkout(
            checkout,
            kit,
            quantity,
            user=user,
            tier_lookup=resolve_tier_lookup(kit, checkout, user),
        )
    except pricing.KitRefusal as refusal:
        return _refused(str(refusal))

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
