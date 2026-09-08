# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Which price a dealer pays, and how many queries it costs to find out.

One query answers a whole product-listing page. The buyer's group is reached by
JOIN from the tier rows rather than by a lookup first, so asking about 100
variants is one round trip and not two; the retail price each row must sit under
rides along as a correlated subquery in the same statement. Design budget,
section 5: "Dealer display batch: 1 query for up to 100 variants."

The retail floor is applied HERE, once, rather than at each caller: a tier row
above the channel's retail price is not an offer, so it never reaches a ladder
and never reaches a line. Refusing rather than clamping is Compose's rule for
option-value tiers (`AboveRetailError`); a variant tier is a whole price rather
than a delta, and a shopper standing on a product page is the wrong audience for
a merchant's data bug, so here the bad row is simply not offered and the buyer
falls back to retail.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import NamedTuple

import graphene
from django.conf import settings
from django.db.models import OuterRef, Subquery

from ...product.models import ProductVariantChannelListing
from .models import TierPrice

# Endpoint 3's cap. A storefront asking about more variants than a page can show
# is a bug in the caller, and an unbounded IN () is how one becomes an outage.
MAX_BATCH = 100


class DealerPrice(NamedTuple):
    """One break. Ordered so `amount, min_quantity = ...` reads as it should."""

    amount: Decimal
    min_quantity: int
    group_code: str


def ladders(
    user, channel, variant_ids, *, database_connection_name=None
) -> dict[int, list[DealerPrice]]:
    """Every break these variants offer this buyer, by variant pk, in ONE query.

    Empty for a shopper who is not a dealer, which is the same answer as a dealer
    with no rows on these variants: endpoint 3 does not distinguish them and
    neither should this.

    Reads the REPLICA by default. This is a display query on a product page, the
    hottest caller in the app, and the answer is a catalog fact rather than a
    cart fact, so replica lag costs nothing a merchant would notice. The two
    endpoints that go on to write a line pass the writer instead, because a
    price they are about to stamp on a checkout line has to be read from the
    same database the line is written to.
    """
    if not variant_ids or user is None or not getattr(user, "is_authenticated", False):
        return {}

    db = database_connection_name or settings.DATABASE_CONNECTION_REPLICA_NAME

    retail = ProductVariantChannelListing.objects.filter(
        variant_id=OuterRef("variant_id"), channel_id=channel.pk
    ).values("price_amount")[:1]

    rows = (
        TierPrice.objects.using(db)
        .filter(variant_id__in=variant_ids, group__customers__user=user)
        .annotate(retail_amount=Subquery(retail))
        .order_by("variant_id", "min_quantity")
        .values_list(
            "variant_id", "min_quantity", "amount", "group__code", "retail_amount"
        )
    )

    found: dict[int, list[DealerPrice]] = defaultdict(list)
    for variant_id, min_quantity, amount, group_code, retail_amount in rows:
        if retail_amount is not None and amount > retail_amount:
            continue
        found[variant_id].append(DealerPrice(amount, min_quantity, group_code))
    return dict(found)


def best_break(breaks, quantity: int) -> DealerPrice | None:
    """The highest break this quantity reaches. None when it reaches none."""
    winner = None
    for candidate in breaks:
        if candidate.min_quantity <= quantity and (
            winner is None or candidate.min_quantity > winner.min_quantity
        ):
            winner = candidate
    return winner


def dealer_price_for(
    variant, user, quantity: int, *, channel, database_connection_name=None
) -> DealerPrice | None:
    """What this buyer pays each for `quantity` of `variant`, or None at retail.

    `channel` is required and keyword-only: the retail floor is a per-channel
    fact, so there is no honest single-channel default to hide.
    """
    variant_id = getattr(variant, "pk", variant)
    breaks = ladders(
        user, channel, [variant_id], database_connection_name=database_connection_name
    ).get(variant_id, [])
    return best_break(breaks, quantity)


def prices_for_variants(
    user, channel, variant_ids, *, database_connection_name=None
) -> dict[str, list[dict]]:
    """Endpoint 3's body: `{variant GID: [{minQuantity, amount}]}`, one query.

    Amounts are quantized to the channel's currency and rendered as strings, so
    a JavaScript client never parses money into a float.
    """
    currency = channel.currency_code
    found = ladders(
        user, channel, variant_ids, database_connection_name=database_connection_name
    )
    return {
        graphene.Node.to_global_id("ProductVariant", variant_id): [
            {
                "minQuantity": row.min_quantity,
                "amount": str(_quantized(row.amount, currency)),
            }
            for row in rows
        ]
        for variant_id, rows in found.items()
    }


def _quantized(amount: Decimal, currency: str) -> Decimal:
    from ...core.prices import quantize_price

    return quantize_price(amount, currency)
