# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one endpoint a kit needs: put the members in the checkout, priced by us.

There is no GraphQL in this unit and no new field on Collection. A series
collection page is the STOCK collection page; everything the storefront needs to
draw the configurator is on the Collection's own metadata under `wsm.series`
(see models.SeriesConfig.stamp_collection), which Saleor already returns.
"""

import base64
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
from ..http import (
    buyer_mismatch,
    global_pk,
    refuses_malformed_ids,
    storefront_key_required,
)
from . import pricing
from .models import KitConfig, KitRulesRefused, UnknownKitMember


def _money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


# The app's own error shape, handed to the shared refusal once. This module's
# own byte-identical copy of `_from_gid` is gone: the pk-shape check that keeps
# a junk id out of the ORM exists once, in saleor/wsm/http.py.
_MALFORMED = refuses_malformed_ids(lambda message: {"violations": [message]})


def _to_gid(type_name: str, pk) -> str:
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


def _not_found(what: str):
    return JsonResponse({"violations": [f"unknown {what}"]}, status=404)


def _refused(message: str):
    return JsonResponse({"violations": [message]}, status=422)


def _variant_pk(raw):
    """One posted part, as a primary key, or None.

    A global id is what the storefront sends and what this endpoint answers
    with. A bare id is accepted too: it costs one branch and it is the shape
    every other caller of a REST endpoint reaches for first. Neither is trusted
    past this point, because the pk still has to name a member of THIS kit.
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    pk = global_pk(raw or "", "ProductVariant")
    return int(pk) if pk is not None else None


def _rule_json(rule):
    """One cross-member rule as the storefront reads it.

    Variants are named by the same global ids the rest of this response uses, so
    a storefront pairs a rule to a line it already holds without a second call.
    """
    targets = [
        _to_gid("ProductVariant", variant_id) for variant_id in rule.target_variant_ids
    ]
    subject = _to_gid("ProductVariant", rule.subject.variant_id)
    return {
        "kind": rule.kind,
        "message": rule.message,
        "subject": subject,
        "targets": targets,
        # Every part the rule speaks about, subject first: a page that only
        # wants to mark the rows a refusal is about reads this and nothing else.
        "variantIds": [subject, *targets],
    }


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
        # `.all()` rather than `.values_list`, so a caller that prefetched
        # the members (MP3 does, for every kit on the cart at once) does not
        # buy a second read of rows it is already holding.
        [member.variant_id for member in kit.members.all()],
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
@_MALFORMED
def kit_line(request):
    """Explode one kit into its member lines at server-computed prices."""
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _refused("body is not JSON")

    checkout_token = global_pk(body.get("checkoutId", ""), "Checkout", shape="uuid")
    collection_pk = global_pk(body.get("collectionId", ""), "Collection")
    if checkout_token is None:
        return _not_found("checkout")
    if collection_pk is None:
        return _not_found("kit")

    quantity = whole_number(body.get("quantity") or 1)
    if quantity is None:
        return _refused("quantity must be a whole number")
    if quantity < 1:
        return _refused("quantity must be at least 1")

    # The shopper's picks. Absent is the kit as the merchant built it; present
    # and empty is a page that let someone pick nothing, which has no price.
    raw_picks = body.get("variantIds")
    variant_ids = None
    if raw_picks is not None:
        if not isinstance(raw_picks, list):
            return _refused("variantIds must be a list of parts")
        if not raw_picks:
            return _refused("choose at least one part of this kit")
        variant_ids = [_variant_pk(raw) for raw in raw_picks]
        if any(pk is None for pk in variant_ids):
            return _not_found("part")

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
        customer_pk = global_pk(raw_customer, "User")
        if customer_pk is None:
            return _not_found("customer")
        # A checkout that names a user is evidence about the buyer that the body
        # cannot overrule. The storefront never sends a mismatched pair.
        if buyer_mismatch(checkout, customer_pk):
            return JsonResponse(
                {"violations": ["this checkout belongs to another buyer"]}, status=409
            )
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
        group_id, priced, lines_by_variant, rules, fees = pricing.add_kit_to_checkout(
            checkout,
            kit,
            quantity,
            user=user,
            tier_lookup=tier_lookup,
            tier_group=tier_group,
            variant_ids=variant_ids,
        )
    except KitRulesRefused as refusal:
        # The merchant wrote these sentences for a shopper, so they are what the
        # shopper is shown; the code beside them is what a storefront branches
        # on, and it never changes with the wording.
        return JsonResponse(
            {
                "violations": [rule.message for rule in refusal.rules],
                "code": refusal.code,
                "rules": [_rule_json(rule) for rule in refusal.rules],
            },
            status=422,
        )
    except UnknownKitMember as refusal:
        return JsonResponse(
            {
                "violations": [str(refusal)],
                "code": refusal.code,
                "variantIds": [
                    _to_gid("ProductVariant", variant_id)
                    for variant_id in refusal.variant_ids
                ],
            },
            status=422,
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
            # What the picked parts cost before the kit did anything, and the
            # saving as its OWN row. The rail used to be handed two numbers and
            # had to subtract them to name a discount; a page that computes a
            # discount is a page that can disagree with the till. `amount` is
            # the sum of the per-line spreads by construction, tier lines
            # included, so list less amount IS the kit total, to the cent.
            "listTotal": _money(priced.list_total_cents),
            "discount": {
                "label": kit.discount_label,
                "percent": kit.discount_percent,
                "amount": _money(priced.list_total_cents - priced.total_cents),
                "listTotal": _money(priced.list_total_cents),
            },
            # The kit total is the MEMBERS. A charge is money on top of them, it
            # is not what the kit discount was computed on, and folding it into
            # the same number would quietly discount a core deposit.
            "feeTotal": _money(sum(fee.total_cents for fee in fees)),
            "fees": [
                {
                    "lineId": (
                        _to_gid("CheckoutLine", line.pk)
                        if (line := lines_by_variant.get(fee.variant.pk))
                        else None
                    ),
                    "variantId": _to_gid("ProductVariant", fee.variant.pk),
                    "parentVariantId": _to_gid("ProductVariant", fee.parent_variant_id),
                    "label": fee.label,
                    "sku": fee.sku,
                    # The whole charge on this line, which is what a cart row
                    # shows, and the unit and count it is made of.
                    "amount": _money(fee.total_cents),
                    "unitPrice": _money(fee.unit_cents),
                    "quantity": fee.quantity,
                    "applyTo": fee.apply_to,
                }
                for fee in fees
            ],
            # Read off the rows the add already had to load, so a kit that
            # carries no rules pays nothing for the key being here.
            #
            # ponytail: this is the only public place a kit's rules appear, so a
            # PDP cannot draw them until something is added. The ceiling is a
            # `wsm.kit` stamp on the Collection, mirroring `wsm.series`; the
            # upgrade is worth buying the first time a storefront asks to show
            # the rule BEFORE the add rather than instead of it.
            "rules": [_rule_json(rule) for rule in rules],
        }
    )
