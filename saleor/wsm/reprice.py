# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""MP3: every price this fork owns is re-derived from this fork's own tables.

The bug this closes is not a wrong formula. It is a stored number outliving the
facts it was computed from. `CheckoutLine.price_override` is the native seam
(design doc section 3) and it is a COLUMN: once a dealer break or a configured
unit is written there it stays there, and until now the only thing that ever
recomputed it was the storefront choosing to call the reprice endpoint. So a
buyer could add ten at the ten-up break, drop the line to one, and complete at
the ten-up price; and a per-unit crating fee, which is its own line, kept the
quantity it was added with while its parent line moved.

The fix is to stop believing the column. Whenever Saleor recalculates a
checkout's prices it now first re-derives, from our tables and the CURRENT line
quantities and the CURRENT checkout customer, every override we wrote, and
corrects the ones that moved.

The seam. `_fetch_checkout_prices_if_expired` is the one funnel every price
recalculation goes through, and it is reached from both completion paths and
from every checkout read. Running here rather than at completion is what makes
this cheap AND complete: prices are recalculated only when they are expired, and
`invalidate_checkout` expires them on every mutation that can move a line, so
the correction lands once per real change instead of once per request, and a
completion whose lines moved cannot get past it. The alternatives were read and
rejected: `preprocess_order_creation` is the only plugin-manager hook at
completion and on the payment path it fires AFTER the order totals are computed,
so it can refuse but cannot correct; the price plugin hooks only run under the
TAX_APP strategy (design doc section 3); and `add_variants_to_checkout` sees a
quantity change but never a catalog change, a sign-in, or a completion.

Correct, do not refuse, when the price simply moved: the shopper sees the right
total in the cart and the order, which is what a merchant wants and what a
5.0 store did. Refuse only when the price cannot be DERIVED at all (a snapshot
that will not parse, an option value the merchant has since deleted, a fee line
with no fee behind it): the safe move on money we cannot re-derive is to stop
the sale, not to guess, and quietly falling back to the variant's base price
would undercharge every configured line. That refusal reaches a cart read too,
which is intended: a line nobody can price is not a line to keep selling.

Cost. Zero queries for a checkout that holds no line of ours, decided from
metadata already loaded on the line. For one that does: one query for the dealer
ladder across every dealer line at once, and for configured lines one option-set
query, one fee query and one buyer-group query for the whole checkout, never one
per line. Base prices come off `CheckoutLineInfo.channel_listing`, which is
already in hand. One `bulk_update` when anything moved, and none when nothing
did, which is the common case.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..checkout.error_codes import CheckoutErrorCode
from ..checkout.models import CheckoutLine
from ..core.db.connection import allow_writer
from . import patches
from .compose import pricing as compose_pricing
from .compose.models import Fee, OptionSet, to_cents
from .compose.views import (
    META_ACCEPTED,
    META_CID,
    META_FEE,
    META_OPTIONS,
    META_PARENT,
    META_SKU,
    PRICE_OVERRIDE_REASON as COMPOSE_REASON,
)
from .dealer import pricing as dealer_pricing
from .dealer.no_stacking import (
    LINE_METADATA_KEY as DEALER_META,
    PRICE_OVERRIDE_REASON as DEALER_REASON,
)

TARGET = "saleor.checkout.calculations._fetch_checkout_prices_if_expired"

# The line fields this file is allowed to move. Named once so the bulk_update
# and the reader agree on the blast radius.
WRITTEN_FIELDS = (
    "price_override",
    "price_override_reason",
    "quantity",
    "metadata",
    "private_metadata",
)

# Every stamp this fork prices from lives in PRIVATE metadata, and this file
# reads nothing else. Stock Saleor maps CheckoutLine PUBLIC metadata to
# `no_permissions` (saleor/graphql/meta/permissions.py), so any unauthenticated
# caller can `updateMetadata` their own line: a stamp we price from in public
# metadata is a price the shopper picks. Measured before this moved: an
# anonymous cart stamped `wsm.dealer` with a guessed group code was priced at
# that group's tier, a 10.00 line at 1.00, with no key and no login.
#
# The compose and containers keys keep a public copy too, written by the same
# views, because the storefront cart and order screens pair fee lines and render
# chosen options off them (wsm-storefront src/lib/composeFee.ts and
# src/lib/order-grouping.ts) and B6 is "no storefront code". Those copies are
# DISPLAY only: forging one changes what a shopper's own cart draws and no
# number anywhere. `wsm.dealer` has no storefront reader, so it has no public
# copy, and one found in public metadata is a forgery and is deleted on sight.

# `CheckoutLineInfo` memoises the prices it derives from the line. Correcting the
# line without dropping these would leave the calculation running on the numbers
# we just replaced.
CACHED_ON_LINE_INFO = (
    "variant_discounted_price",
    "undiscounted_unit_price",
    "prior_unit_price_amount",
)

_installed = False


class Unrepriceable(Exception):
    """A line we own whose price cannot be re-derived from the catalog at all."""


def install() -> None:
    """Called once from `WsmConfig.ready()`."""
    global _installed
    if _installed:
        return
    _installed = True
    patches.install_guard(TARGET, guard)


def guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def _fetch_checkout_prices_if_expired(
        checkout_info,
        manager,
        lines,
        allow_sync_webhooks,
        requestor,
        force_update: bool = False,
        database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
    ):
        # The original's own early return, read from the same two facts. Running
        # on a checkout whose prices are still fresh would re-derive on every
        # field of every cart query for a saving of nothing: fresh means the
        # last recalculation already ran this, and nothing has invalidated it
        # since.
        if force_update or checkout_info.checkout.price_expiration <= timezone.now():
            reprice(checkout_info, lines)
        return original(
            checkout_info=checkout_info,
            manager=manager,
            lines=lines,
            allow_sync_webhooks=allow_sync_webhooks,
            requestor=requestor,
            force_update=force_update,
            database_connection_name=database_connection_name,
        )

    return _fetch_checkout_prices_if_expired


def reprice(checkout_info, lines) -> list:
    """Re-derive every price this fork owns on this checkout. Returns the lines moved.

    Idempotent: the answer is a function of the catalog, the line quantities and
    the checkout's customer, never of what the line was priced at before, so
    running it twice writes once.
    """
    # Reads the WRITER, deliberately, and not the connection the caller was
    # using. `_fetch_checkout_prices_if_expired` runs on the replica on a cart
    # read, and a ladder or an option price read from a lagging replica would
    # be stamped onto a line this function then writes. Every read below backs
    # a write, so every read is a writer read.
    database_connection_name = settings.DATABASE_CONNECTION_DEFAULT_NAME
    moved: list = []

    def mark(line_info):
        for name in CACHED_ON_LINE_INFO:
            line_info.__dict__.pop(name, None)
        if line_info not in moved:
            moved.append(line_info)

    configured, fee_lines, dealer_lines = _classify(lines)
    _disown_forged_stamps(lines, mark)
    if not configured and not fee_lines and not dealer_lines and not moved:
        return []

    # One writer block for the whole pass. Core restricts the writer on a cart
    # request, and every read below is a read that backs a write, so opting in
    # once here is the honest shape rather than sprinkling it over each query.
    try:
        with allow_writer():
            if dealer_lines:
                _reprice_dealer(
                    checkout_info, dealer_lines, database_connection_name, mark
                )
            if configured or fee_lines:
                _reprice_configured(
                    checkout_info,
                    configured,
                    fee_lines,
                    database_connection_name,
                    mark,
                )
            if moved:
                CheckoutLine.objects.bulk_update(
                    [info.line for info in moved], list(WRITTEN_FIELDS)
                )
    except Unrepriceable as problem:
        raise ValidationError(
            {
                "lines": ValidationError(
                    str(problem), code=CheckoutErrorCode.INVALID.value
                )
            }
        ) from problem
    return moved


def _classify(lines):
    """Split the checkout's lines into the three kinds we own. No queries.

    Read from PRIVATE metadata only. A line the fork never priced carries none
    of these keys and is not ours to move: a retail line stays retail here even
    for a dealer, because deciding that a plain line should BECOME a dealer line
    is the storefront's add, not this function's enforcement. A line carrying a
    forged PUBLIC copy of one of these keys is, for the same reason, not ours.
    """
    configured, fee_lines, dealer_lines = [], [], []
    for line_info in lines:
        stamps = line_info.line.private_metadata or {}
        if META_FEE in stamps:
            fee_lines.append(line_info)
        elif META_OPTIONS in stamps:
            configured.append(line_info)
        elif (
            DEALER_META in stamps
            or line_info.line.price_override_reason == DEALER_REASON
        ):
            dealer_lines.append(line_info)
    return configured, fee_lines, dealer_lines


def _disown_forged_stamps(lines, mark):
    """Delete `wsm.dealer` from PUBLIC metadata, which anyone can write.

    Enforcement is above: the authority copy is private and nothing here reads
    the public one. This is the hygiene that goes with it, so a forged key does
    not sit on the row telling a support screen the line is a dealer line while
    the money says it is not, and does not ride into the order. Costs nothing on
    a checkout carrying none, which is every honest checkout.
    """
    for line_info in lines:
        if DEALER_META in (line_info.line.metadata or {}):
            line_info.line.delete_value_from_metadata(DEALER_META)
            mark(line_info)


def _reprice_dealer(checkout_info, dealer_lines, database_connection_name, mark):
    """Re-run the ladder for every dealer line at once, against today's quantity.

    A break the line no longer reaches is REMOVED, not kept: that is the whole
    ten-at-the-break-then-drop-to-one exploit.

    WHO the buyer is comes from `checkout_info.user` when the checkout has one,
    and otherwise from the group already stamped on the line at add time. An
    anonymous checkout priced against a `customerId` the caller supplied is how
    both dealer endpoints and the kit endpoint work today; deciding whether that
    id can be trusted is a different unit, and this function neither widens nor
    narrows it. What it does re-derive, in both cases, is the BREAK, against the
    quantity the line carries now. Still one query either way.
    """
    stamped = {}
    for line_info in dealer_lines:
        raw = (line_info.line.private_metadata or {}).get(DEALER_META)
        code = None
        if raw:
            try:
                code = (json.loads(raw) or {}).get("group")
            except ValueError:
                code = None
        stamped[line_info.line.pk] = code

    user = checkout_info.user
    variant_ids = [info.line.variant_id for info in dealer_lines]
    if user is not None and getattr(user, "is_authenticated", False):
        found = dealer_pricing.ladders(
            user,
            checkout_info.channel,
            variant_ids,
            database_connection_name=database_connection_name,
        )
        by_stamp = None
    else:
        codes = sorted({code for code in stamped.values() if code})
        found = (
            dealer_pricing.ladders(
                None,
                checkout_info.channel,
                variant_ids,
                group_codes=codes,
                database_connection_name=database_connection_name,
            )
            if codes
            else {}
        )
        by_stamp = stamped

    for line_info in dealer_lines:
        line = line_info.line
        before = _snapshot_of(line)
        breaks = found.get(line.variant_id, [])
        if by_stamp is not None:
            # One checkout can hold lines stamped with different groups; a line
            # is only offered the ladder of the group it was priced against.
            breaks = [row for row in breaks if row.group_code == by_stamp.get(line.pk)]
        winner = dealer_pricing.best_break(breaks, line.quantity)
        if winner:
            line.price_override = winner.amount
            line.price_override_reason = DEALER_REASON
            line.store_value_in_private_metadata(
                {
                    DEALER_META: json.dumps(
                        {"group": winner.group_code, "minQuantity": winner.min_quantity}
                    )
                }
            )
        else:
            line.price_override = None
            line.price_override_reason = None
            line.delete_value_from_private_metadata(DEALER_META)
        if before != _snapshot_of(line):
            mark(line_info)


def _reprice_configured(
    checkout_info, configured, fee_lines, database_connection_name, mark
):
    """Re-price each configured line and the fee lines hanging off it.

    The selections are read back out of the line's own `wsm.options` snapshot,
    which is the only record of what the shopper chose; every NUMBER is read
    fresh from the catalog, so a merchant's price change and the buyer's current
    group both reach the line, and the snapshot is rewritten with what was
    actually charged.
    """
    product_ids = {info.product.pk for info in configured}

    sets_by_product = defaultdict(list)
    for option_set in (
        OptionSet.objects.using(database_connection_name)
        .filter(product_id__in=product_ids)
        .prefetch_related("values", "values__tier_deltas")
    ):
        sets_by_product[option_set.product_id].append(option_set)

    fees_by_product, fees_by_id = defaultdict(list), {}
    for fee in Fee.objects.using(database_connection_name).filter(
        product_id__in=product_ids
    ):
        fees_by_product[fee.product_id].append(fee)
        fees_by_id[fee.pk] = fee

    user = checkout_info.user
    tier_group = dealer_pricing.tier_group_for(
        user.pk if user else None, database_connection_name=database_connection_name
    )

    fee_lines_by_parent = defaultdict(dict)
    for line_info in fee_lines:
        parent = (line_info.line.private_metadata or {}).get(META_PARENT)
        if not parent:
            raise Unrepriceable(
                "a fee line on this checkout does not say which item it belongs to"
            )
        fee_lines_by_parent[parent][line_info.line.variant_id] = line_info

    claimed = set()
    for line_info in configured:
        claimed.add(
            _reprice_one_configured(
                line_info,
                sets_by_product,
                fees_by_product,
                fees_by_id,
                fee_lines_by_parent,
                tier_group,
                mark,
            )
        )

    orphans = set(fee_lines_by_parent) - claimed
    if orphans:
        raise Unrepriceable(
            "a charge on this checkout no longer belongs to any item in it"
        )


def _reprice_one_configured(
    line_info,
    sets_by_product,
    fees_by_product,
    fees_by_id,
    fee_lines_by_parent,
    tier_group,
    mark,
):
    line = line_info.line
    stamps = line.private_metadata or {}
    cid = stamps.get(META_CID)
    if not cid:
        raise Unrepriceable("a configured line on this checkout has no identifier")
    if line_info.channel_listing is None or line_info.channel_listing.price_amount is None:
        raise Unrepriceable("a configured item on this checkout is no longer for sale")

    try:
        snapshot = json.loads(stamps[META_OPTIONS])
        accepted = json.loads(stamps.get(META_ACCEPTED) or "[]")
    except ValueError as error:
        raise Unrepriceable(
            "the options recorded on a configured line cannot be read"
        ) from error

    product_fees = fees_by_product.get(line_info.product.pk, [])
    # Exactly what the add endpoint does with the same list: a required fee is
    # charged whether or not it was named, and naming one is not "accepting" it.
    required = {fee.pk for fee in product_fees if fee.required}
    try:
        priced = compose_pricing.price_configured(
            to_cents(line_info.channel_listing.price_amount),
            [
                option_set.to_pricing()
                for option_set in sets_by_product.get(line_info.product.pk, [])
            ],
            _selections_from(snapshot),
            tier_group,
            fees=[fee.to_pricing() for fee in product_fees],
            accepted_fee_ids=tuple(
                fee_id for fee_id in accepted if fee_id not in required
            ),
            base_sku=line_info.variant.sku or "",
            quantity=line.quantity,
        )
    except compose_pricing.ComposeRefusal as refusal:
        raise Unrepriceable(
            f"a configured item on this checkout can no longer be priced: {refusal}"
        ) from refusal

    fresh = json.dumps(priced.snapshot)
    before = _snapshot_of(line)
    line.price_override = Decimal(priced.unit_cents) / 100
    line.price_override_reason = COMPOSE_REASON
    line.store_value_in_private_metadata({META_OPTIONS: fresh})
    # The public copy the storefront cart reads. Display, never an input.
    line.store_value_in_metadata({META_OPTIONS: fresh, META_SKU: priced.composite_sku})
    if before != _snapshot_of(line):
        mark(line_info)

    _reprice_fee_lines(
        cid, line.quantity, priced, fees_by_id, fee_lines_by_parent, mark
    )
    return cid


def _reprice_fee_lines(cid, quantity, priced, fees_by_id, fee_lines_by_parent, mark):
    """Make the fee lines say what the pricing engine just charged for them.

    A per-unit fee is one line of the parent's quantity and a per-line fee is one
    line of 1, which is the rule the add endpoint writes and the rule that used
    to hold only until the shopper changed the quantity. The pairing is by fee
    VARIANT, because each fee owns one and the line points at it; the label on
    the line is display text and pairing on it would break the first time a
    merchant renamed a fee.
    """
    present = fee_lines_by_parent.get(cid, {})
    expected = {}
    for row in priced.snapshot["fees"]:
        fee = fees_by_id.get(row["id"])
        if fee is None or fee.variant_id is None:
            raise Unrepriceable("a charge on this checkout no longer has a product")
        expected[fee.variant_id] = row

    if set(expected) != set(present):
        # Adding or dropping a line is a cart mutation, not a recalculation, so
        # the honest answer here is to stop rather than to sell a configured
        # item with a charge missing or a charge nobody is owed.
        raise Unrepriceable(
            "the charges on a configured item in this checkout no longer match it"
        )

    for variant_id, row in expected.items():
        line_info = present[variant_id]
        line = line_info.line
        before = _snapshot_of(line)
        line.quantity = quantity if row["apply_to"] == compose_pricing.PER_UNIT else 1
        line.price_override = Decimal(row["amount"]) / 100
        line.price_override_reason = COMPOSE_REASON
        stamp = json.dumps({"label": row["label"], "apply_to": row["apply_to"]})
        line.store_value_in_private_metadata({META_FEE: stamp})
        # The public copy the storefront cart reads. Display, never an input.
        line.store_value_in_metadata({META_FEE: stamp})
        if before != _snapshot_of(line):
            mark(line_info)


def _selections_from(snapshot):
    """What the shopper chose, read back out of the snapshot the add wrote.

    One entry per option set, values gathered: a multi-choice axis wrote one
    snapshot row per value, and handing the engine one Selection per row would
    trip its own duplicate-selection refusal.
    """
    picked: dict[int, dict] = {}
    for row in snapshot.get("lines") or []:
        set_id = row.get("option_set_id")
        if not isinstance(set_id, int) or isinstance(set_id, bool):
            raise Unrepriceable("an option recorded on a configured line has no id")
        entry = picked.setdefault(set_id, {"value_ids": [], "text": ""})
        if "value_id" in row:
            entry["value_ids"].append(row["value_id"])
        else:
            entry["text"] = row.get("text") or ""
    return [
        compose_pricing.Selection(
            set_id=set_id, value_ids=tuple(entry["value_ids"]), text=entry["text"]
        )
        for set_id, entry in picked.items()
    ]


def _snapshot_of(line):
    """The line as this file is allowed to change it, for a did-anything-move test."""
    return (
        line.price_override,
        line.price_override_reason,
        line.quantity,
        dict(line.metadata or {}),
        dict(line.private_metadata or {}),
    )
