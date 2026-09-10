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
import logging
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.utils import timezone

from ..checkout.models import CheckoutLine
from ..core.db.connection import allow_writer
from . import patches
from .compose import pricing as compose_pricing
from .containers import pricing as kit_pricing
from .compose.models import Fee, OptionSet, to_cents
from .compose.lines import (
    META_ACCEPTED,
    META_CID,
    META_FEE,
    META_OPTIONS,
    META_PARENT,
    META_SKU,
    PRICE_OVERRIDE_REASON as COMPOSE_REASON,
)
from .containers.pricing import (
    META_KIT,
    PRICE_OVERRIDE_REASON as KIT_REASON,
)
from .money import unit_amount
from .dealer import pricing as dealer_pricing
from .dealer.no_stacking import (
    LINE_METADATA_KEY as DEALER_META,
    PRICE_OVERRIDE_REASON as DEALER_REASON,
)

TARGET = "saleor.checkout.calculations._fetch_checkout_prices_if_expired"

# The line fields this file is allowed to move. Named once so the bulk_update
# and the reader agree on the blast radius.
# What this funnel writes on EVERY line it touches. `quantity` is deliberately
# absent: core loads the line objects handed to us on the REPLICA, so their
# quantity is whatever that replica last saw, and writing it back turned a price
# correction into a silent undo of the quantity the shopper had just committed
# in another request. The only lines whose quantity this file owns are fee lines
# (a per-unit fee is one line of the parent's quantity), and those are written
# separately, by pk, and only when the number actually moved.
WRITTEN_FIELDS = (
    "price_override",
    "price_override_reason",
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


logger = logging.getLogger(__name__)


class Unrepriceable(Exception):
    """A line we own whose price cannot be re-derived from the catalog at all.

    Internal to this module and never raised past `reprice()`: it is the signal
    that drives `_drop`, not a refusal. A cart READ that raises is a cart nobody
    can render, repair or empty, and this funnel runs on every read.
    """


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
    requantified: list = []

    def mark(line_info, quantity=False):
        for name in CACHED_ON_LINE_INFO:
            line_info.__dict__.pop(name, None)
        if line_info not in moved:
            moved.append(line_info)
        if quantity and line_info.line not in requantified:
            requantified.append(line_info.line)

    configured, fee_lines, kit_lines, dealer_lines = _classify(lines)
    _disown_forged_stamps(lines, mark)
    if not (configured or fee_lines or kit_lines or dealer_lines or moved):
        return []

    # One writer block for the whole pass. Core restricts the writer on a cart
    # request, and every read below is a read that backs a write, so opting in
    # once here is the honest shape rather than sprinkling it over each query.
    with allow_writer():
        if dealer_lines:
            _reprice_dealer(checkout_info, dealer_lines, database_connection_name, mark)
        dropped = []
        if kit_lines:
            dropped += _reprice_kits(
                checkout_info, kit_lines, database_connection_name, mark
            )
        if configured or fee_lines:
            dropped += _reprice_configured(
                checkout_info,
                configured,
                fee_lines,
                database_connection_name,
                mark,
            )
        if dropped:
            _drop(dropped)
        if moved:
            CheckoutLine.objects.bulk_update(
                [info.line for info in moved], list(WRITTEN_FIELDS)
            )
        if requantified:
            CheckoutLine.objects.bulk_update(requantified, ["quantity"])
    return moved


def _drop(dropped):
    """Take lines off the checkout that this fork can no longer price. One query.

    Reading a cart must never raise. This funnel runs on every price
    recalculation, and a recalculation is what a cart READ is, so an exception
    here does not warn a shopper about one line: it makes the whole checkout
    impossible to render, impossible to repair and impossible to empty. That
    wedge was reachable with the stock remove button on a fee line.

    The rows go, and the in-flight calculation keeps the objects, deliberately.
    The GraphQL layer has already loaded these lines for this request and
    resolves a non-nullable `unitPrice` off the very `CheckoutLineInfo` list
    handed to us, so pulling entries out of it mid-resolve turns one wedge into
    another. They are priced at nothing instead: this render still shows the
    line, the TOTAL it shows is already the total without it, the next read does
    not see the row at all, and completion re-fetches from the table, so nothing
    that no longer exists can be charged for.
    """
    for line_info in dropped:
        line_info.line.price_override = Decimal(0)
        for name in CACHED_ON_LINE_INFO:
            line_info.__dict__.pop(name, None)
    CheckoutLine.objects.filter(pk__in={info.line.pk for info in dropped}).delete()


def _reprice_kits(checkout_info, kit_lines, database_connection_name, mark):
    """Re-derive every kit member line through the kit's own money.

    A member line is an ORDINARY checkout line by design, and its price is not an
    ordinary price: it is the member's prorated share of the kit's discount, or a
    dealer tier where that is cheaper. Nothing re-derived it. A member that took
    no tier carried whatever the add stamped on it for the life of the cart, so a
    merchant who changed the kit's discount, or a member's list price, sold the
    old number to every cart already holding one. A member that DID take a tier
    fell to `_reprice_dealer`, which knows only the flat per-variant ladder: when
    the merchant then withdrew that tier, the line did not fall back to the kit
    price it was still entitled to, it fell all the way back to LIST, and the
    shopper was overcharged the whole kit discount on that member.

    Members are re-priced together, from the kit, because that is the only way
    the proration is right; the ones still IN the cart take their unit from that
    answer. A shopper who deleted a member keeps the others at their own prices,
    which is the kits ruling, not an accident.

    Cost: FOUR queries per distinct kit on the checkout (the kit, its members,
    their channel prices, and one ladder read covering every member), and zero
    on a checkout carrying none.
    """
    from .containers.models import KitConfig
    from .containers.views import resolve_tier_lookup

    token = checkout_info.checkout.token
    user = checkout_info.user
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None

    groups: dict[tuple, list] = defaultdict(list)
    dropped = []
    charges: dict[tuple, list] = defaultdict(list)
    for line_info in kit_lines:
        try:
            stamp = json.loads(line_info.line.private_metadata[META_KIT]) or {}
            # The picks are part of the identity of what was bought: two adds of
            # the same kit with different parts are two kits, priced apart.
            key = (
                str(stamp["collection"]),
                int(stamp["quantity"]),
                tuple(sorted(int(v) for v in stamp.get("variants") or ())),
            )
        except (KeyError, TypeError, ValueError):
            _log_drop(token, line_info, "this item does not say which kit priced it")
            dropped.append(line_info)
            continue
        if META_FEE in (line_info.line.private_metadata or {}):
            charges[key].append(line_info)
            # A kit that is nothing BUT charges is not a kit any more; the group
            # still has to be visited so those lines go with it.
            groups.setdefault(key, [])
            continue
        groups[key].append(line_info)

    for (slug, quantity, picks), infos in groups.items():
        try:
            kit = KitConfig.objects.filter(collection__slug=slug).first()
            if kit is None:
                raise kit_pricing.KitRefusal(f"the kit {slug} no longer exists")
            priced = kit_pricing.price_kit(
                kit.pricing_members(checkout_info.channel, picks or None),
                kit.discount_kind,
                kit.discount_amount,
                kit_quantity=quantity,
                tier_lookup=resolve_tier_lookup(
                    kit, checkout_info.checkout, user, group_code=_kit_group(infos)
                ),
                user=user,
            )
        except kit_pricing.KitRefusal as problem:
            for line_info in infos:
                _log_drop(token, line_info, str(problem))
            dropped.extend(infos)
            continue

        # The kit's charges, re-derived from the same priced members, and only
        # when the checkout is carrying one: a kit whose parts have no fees pays
        # nothing for this. A charge that is no longer owed goes; a REQUIRED one
        # whose line the shopper deleted takes the kit with it, because the kit
        # is not sellable without it and selling it anyway is the undercharge.
        kit_charges = charges.get((slug, quantity, picks)) or []
        if kit_charges:
            try:
                expected = _kit_charges(priced, database_connection_name)
            except kit_pricing.KitRefusal as problem:
                for line_info in infos + kit_charges:
                    _log_drop(token, line_info, str(problem))
                dropped.extend(infos + kit_charges)
                continue
            parents = {
                (info.line.private_metadata or {}).get(META_CID) for info in infos
            }
            present = {info.line.variant_id for info in kit_charges}
            if set(expected) - present:
                for line_info in infos + kit_charges:
                    _log_drop(
                        token,
                        line_info,
                        "a required charge on this kit can no longer be taken",
                    )
                dropped.extend(infos + kit_charges)
                continue
            for line_info in kit_charges:
                stamps = line_info.line.private_metadata or {}
                if stamps.get(META_PARENT) not in parents:
                    _log_drop(token, line_info, "the part this charge belongs to is gone")
                    dropped.append(line_info)
                    continue
                owed = expected.get(line_info.line.variant_id)
                if owed is None:
                    _log_drop(token, line_info, "this charge is no longer owed")
                    dropped.append(line_info)
                    continue
                row, fee_quantity = owed
                _write_fee_line(line_info, row, fee_quantity, mark)

        by_variant = {row.member.variant.pk: row for row in priced.lines}
        for line_info in infos:
            line = line_info.line
            row = by_variant.get(line.variant_id)
            if row is None:
                _log_drop(token, line_info, "this item is no longer part of its kit")
                dropped.append(line_info)
                continue
            before = _snapshot_of(line)
            line.price_override = Decimal(row.unit_cents) / 100
            line.price_override_reason = DEALER_REASON if row.on_tier else KIT_REASON
            if row.on_tier:
                line.store_value_in_private_metadata(
                    {DEALER_META: line.private_metadata.get(DEALER_META) or "{}"}
                )
            else:
                # The tier went away, so the line stops being a dealer line and
                # a voucher may reach it again.
                line.delete_value_from_private_metadata(DEALER_META)
            if before != _snapshot_of(line):
                mark(line_info)
    return dropped


def _kit_charges(priced, database_connection_name):
    """`{fee variant id: (row, line quantity)}` for a priced kit. ONE fee query.

    The same function the add charged through (`containers.pricing`), so a
    charge cannot be quoted at the till and re-derived differently on the next
    cart read.
    """
    return {
        fee.variant_id: (row, quantity)
        for fee, row, quantity, _parent in kit_pricing.kit_fee_rows(
            priced,
            kit_pricing.member_fees(priced, database_connection_name),
        )
        if fee.variant_id is not None
    }


def _write_fee_line(line_info, row, quantity, mark):
    """Make one fee line say what the pricing engine just charged for it."""
    line = line_info.line
    before = _snapshot_of(line)
    was = line.quantity
    line.quantity = quantity
    line.price_override = Decimal(row["amount"]) / 100
    line.price_override_reason = COMPOSE_REASON
    stamp = json.dumps({"label": row["label"], "apply_to": row["apply_to"]})
    line.store_value_in_private_metadata({META_FEE: stamp})
    # The public copy the storefront cart reads. Display, never an input.
    line.store_value_in_metadata({META_FEE: stamp})
    if before != _snapshot_of(line):
        mark(line_info, quantity=line.quantity != was)


def _kit_group(infos):
    """The dealer group these member lines were priced against, if any.

    One group per kit add, so the first line that carries one answers for all.
    """
    for line_info in infos:
        raw = (line_info.line.private_metadata or {}).get(DEALER_META)
        if not raw:
            continue
        try:
            code = (json.loads(raw) or {}).get("group")
        except ValueError:
            continue
        if code:
            return code
    return None


def _classify(lines):
    """Split the checkout's lines into the three kinds we own. No queries.

    Read from PRIVATE metadata only. A line the fork never priced carries none
    of these keys and is not ours to move: a retail line stays retail here even
    for a dealer, because deciding that a plain line should BECOME a dealer line
    is the storefront's add, not this function's enforcement. A line carrying a
    forged PUBLIC copy of one of these keys is, for the same reason, not ours.
    """
    configured, fee_lines, kit_lines, dealer_lines = [], [], [], []
    for line_info in lines:
        stamps = line_info.line.private_metadata or {}
        if META_FEE in stamps:
            # A charge on a KIT member is re-derived from the kit, because that
            # is where its money comes from; it has no configured parent to hang
            # off and used to be dropped as an orphan on the first cart read.
            if META_KIT in stamps:
                kit_lines.append(line_info)
            else:
                fee_lines.append(line_info)
        elif META_OPTIONS in stamps:
            configured.append(line_info)
        elif META_KIT in stamps:
            # Before the dealer test, deliberately: a kit member that took a
            # tier carries BOTH stamps, and its price is the kit's arithmetic
            # rather than the plain per-variant ladder.
            kit_lines.append(line_info)
        elif (
            DEALER_META in stamps
            or line_info.line.price_override_reason == DEALER_REASON
        ):
            dealer_lines.append(line_info)
    return configured, fee_lines, kit_lines, dealer_lines


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
    # A checkout with a user on it knows its buyer, and that buyer's group is the
    # only authority: a stamp is never allowed to promote a signed-in retail
    # shopper. A checkout WITHOUT one is the storefront's normal shape, because
    # the key-gated add resolves the customer server side and never attaches
    # them, so there the group the add stamped stands, exactly as it already
    # does for a plain dealer line (`_reprice_dealer`). The stamp is private, so
    # it is ours and not the shopper's.
    stamped_group_stands = user is None

    # The group each line is priced at, decided once. It picks the option
    # deltas AND the base the line starts from, so it is read before either.
    groups = {
        info.line.pk: (_stamped_group(info.line) if stamped_group_stands else tier_group)
        for info in configured
    }
    codes = sorted({code for code in groups.values() if code})
    # One query for every dealer-priced line on the checkout, the shape
    # `_reprice_dealer` already uses, and no query at all for a cart with no
    # dealer on it. One checkout can hold lines stamped with different groups,
    # so each line is offered only the ladder of its own.
    tiers = (
        dealer_pricing.ladders(
            None,
            checkout_info.channel,
            [info.line.variant_id for info in configured],
            group_codes=codes,
            database_connection_name=database_connection_name,
        )
        if codes
        else {}
    )

    token = checkout_info.checkout.token
    dropped = []

    fee_lines_by_parent = defaultdict(dict)
    for line_info in fee_lines:
        parent = (line_info.line.private_metadata or {}).get(META_PARENT)
        if not parent:
            _log_drop(token, line_info, "the charge does not say what it belongs to")
            dropped.append(line_info)
            continue
        fee_lines_by_parent[parent][line_info.line.variant_id] = line_info

    claimed = set()
    for line_info in configured:
        cid = (line_info.line.private_metadata or {}).get(META_CID)
        try:
            claimed.add(
                _reprice_one_configured(
                    line_info,
                    sets_by_product,
                    fees_by_product,
                    fees_by_id,
                    fee_lines_by_parent,
                    groups[line_info.line.pk],
                    [
                        row
                        for row in tiers.get(line_info.line.variant_id, [])
                        if row.group_code == groups[line_info.line.pk]
                    ],
                    mark,
                    dropped,
                )
            )
        except Unrepriceable as problem:
            # No correction can invent what this line should cost, so it stops
            # being on the checkout. Its charges go with it: a crate with
            # nothing to crate is not a thing anyone owes money for.
            _log_drop(token, line_info, str(problem))
            dropped.append(line_info)
            dropped.extend(fee_lines_by_parent.pop(cid, {}).values())

    for orphaned in set(fee_lines_by_parent) - claimed:
        for line_info in fee_lines_by_parent[orphaned].values():
            _log_drop(token, line_info, "the item this charge belongs to is gone")
            dropped.append(line_info)
    return dropped


def _stamped_group(line):
    """The buyer group a configured line was priced against at add time, or None.

    The snapshot is PRIVATE metadata, so it is ours and not the shopper's; the
    public copy beside it is display only. Unreadable is None: a line whose
    record of its own group cannot be parsed prices at retail here and is
    refused for real a moment later in `_reprice_one_configured`, which is the
    one place that turns an unreadable snapshot into a dropped line.
    """
    raw = (line.private_metadata or {}).get(META_OPTIONS)
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("tier_group") or None
    except ValueError:
        return None


def _log_drop(token, line_info, reason):
    logger.warning(
        "wsm reprice: dropping checkout line %s from checkout %s: %s",
        line_info.line.pk,
        token,
        reason,
    )


def _reprice_one_configured(
    line_info,
    sets_by_product,
    fees_by_product,
    fees_by_id,
    fee_lines_by_parent,
    tier_group,
    breaks,
    mark,
    dropped,
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
    # The same base the add endpoint used, re-derived: a promotion that started
    # after the line was added moves it DOWN on the next cart read, one that
    # ended moves it back up, and the dealer's own price is re-read against
    # today's quantity, which is what "no price this fork wrote outlives the
    # facts it was computed from" means.
    base_amount, base_tiered = dealer_pricing.base_from_breaks(
        unit_amount(line_info.channel_listing), breaks, line.quantity
    )

    try:
        priced = compose_pricing.price_configured(
            to_cents(base_amount),
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
            base_tiered=base_tiered,
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
    _mark_dealer(line, priced.snapshot)
    # The public copy the storefront cart reads. Display, never an input.
    line.store_value_in_metadata({META_OPTIONS: fresh, META_SKU: priced.composite_sku})
    if before != _snapshot_of(line):
        mark(line_info)

    _reprice_fee_lines(
        cid, line.quantity, priced, fees_by_id, fee_lines_by_parent, mark, dropped
    )
    return cid


def _mark_dealer(line, snapshot):
    """A configured line that took a tier IS a dealer line, and says so.

    MP1 and MP2 find a dealer line by the presence of this key
    (`dealer.no_stacking.is_dealer_line`) and nothing else. Without it a
    catalogue promotion or a SPECIFIC_PRODUCT voucher comes off a price that is
    already the dealer's, and the dealer collects both, which "better of, never
    both" exists to refuse. Written here as well as at the add, because a tier
    can start or stop applying between the two: a quantity change, a group
    change, or the merchant deleting the row.
    """
    if snapshot.get("tier_applied"):
        line.store_value_in_private_metadata(
            {DEALER_META: json.dumps({"group": snapshot.get("tier_group") or ""})}
        )
    else:
        line.delete_value_from_private_metadata(DEALER_META)


def _reprice_fee_lines(
    cid, quantity, priced, fees_by_id, fee_lines_by_parent, mark, dropped
):
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
        if fee is not None and fee.variant_id in present:
            expected[fee.variant_id] = row
            continue
        # The line this charge is written on is not on the checkout: deleted
        # with the stock `checkoutLinesDelete`, or the merchant unhooked the fee
        # product. Adding a line back is a cart mutation and this is a
        # recalculation, so it cannot be put right here. An OPTIONAL charge is
        # then simply declined, which is a choice the shopper is allowed to
        # make. A REQUIRED one is not: the configured item is not sellable
        # without it, and selling it anyway is the undercharge that deleting a
        # crate line would otherwise buy. The item goes instead.
        if fee is None or fee.required:
            raise Unrepriceable("a required charge on this item can no longer be taken")

    # A charge nobody is owed any more, because the configuration stopped
    # triggering it. Its line goes; the item it hangs off does not.
    dropped.extend(
        info for variant_id, info in present.items() if variant_id not in expected
    )

    for variant_id, row in expected.items():
        _write_fee_line(
            present[variant_id],
            row,
            quantity if row["apply_to"] == compose_pricing.PER_UNIT else 1,
            mark,
        )


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
