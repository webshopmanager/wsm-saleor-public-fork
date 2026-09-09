# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Kit money: the proration spec from docs/TEAM-BRIEF-containers.md, in int cents.

Everything above `add_kit_to_checkout` is a pure function of numbers: no
database, no Django model, so every price in here is testable without a fixture.
The one impure function at the bottom writes the priced lines through Saleor's
own ``add_variants_to_checkout``, the same call ``checkoutLinesAdd`` makes. No
CheckoutLine row is written by hand and no price ever arrives from a browser.

Two rules decide every number:

* the kit discount is prorated across member lines BY LIST PRICE SHARE, in whole
  cents, largest remainder, so the member lines sum to the kit total exactly
  (TEAM-BRIEF "kit money spec", residue to the highest-value line);
* dealer on a kit is BETTER OF, never both (REQUIREMENTS-configured-pricing
  2.3). A member line takes the dealer tier price or the kit-discounted retail
  unit, whichever is lower. Two consequences fall out of that one comparison:
  the kit discount contributes nothing to a line that took a tier, and a dealer
  never pays more than retail on any line.
"""

import json
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .. import money

FIXED = "fixed"
PERCENT = "percent"
DISCOUNT_KINDS = (FIXED, PERCENT)

# Line metadata keys. The storefront and the order screens read these; they are
# contract, not detail. `bundle_kit_group_id` is spelled exactly as the 50
# shipped kit storefronts already spell it.
META_GROUP = "bundle_kit_group_id"
META_KIT = "wsm.kit"

PRICE_OVERRIDE_REASON = "wsm.containers"

_CENT = Decimal("0.01")


class KitRefusal(Exception):
    """A kit whose numbers cannot be charged. Never swallowed, never guessed past."""


# The fork's one rounding rule, HALF_UP. Re-exported because `containers/models`
# and the tests reach it as `pricing.to_cents`.
to_cents = money.to_cents


@dataclass(frozen=True)
class Member:
    """One kit member as the money sees it: a price, a count, and an identity.

    `variant` is a ProductVariant in production and any object in a unit test:
    nothing here reads it, it is handed to `tier_lookup` and to the caller.
    """

    variant: object
    unit_list_cents: int
    quantity: int = 1


@dataclass(frozen=True)
class MemberPrice:
    member: Member
    unit_cents: int
    line_quantity: int
    share: str
    on_tier: bool


@dataclass(frozen=True)
class KitPrice:
    lines: tuple[MemberPrice, ...]
    list_total_cents: int
    discount_cents: int
    total_cents: int


def kit_discount_cents(kind: str, amount, list_total_cents: int) -> int:
    """Compute the kit's own discount, before it is spread over the members."""
    if kind not in DISCOUNT_KINDS:
        raise KitRefusal(f"unknown discount kind: {kind!r}")
    amount = Decimal(amount)
    if amount < 0:
        raise KitRefusal("a kit discount is never negative")
    if kind == PERCENT:
        if amount > 100:
            raise KitRefusal("a kit discount over 100 percent")
        discount = int(
            (Decimal(list_total_cents) * amount / 100).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    else:
        discount = to_cents(amount)
    if discount >= list_total_cents:
        raise KitRefusal("the kit discount leaves nothing to charge")
    return discount


def prorate(members, discount_cents: int) -> tuple[int, ...]:
    """Per-UNIT discount for each member, by list price share, largest remainder.

    The unit is what a checkout line can carry (`price_override` is a unit
    price), so the allocation is made in units and the exactness that matters is
    ``sum(unit_discount_i * quantity_i) == discount_cents``. With every member
    quantity at 1, which is every kit anyone has shown us, this reduces to the
    textbook largest-remainder split. Ties break to the higher list price, then
    to declaration order: the residue goes to the highest-value line, as the kit
    money spec says.

    When a member quantity exceeds 1 the last few cents of a discount can be
    indivisible by that quantity (3 cents over a member of quantity 2, or the
    whole of a 1 cent discount over two $1 units). Those cents are not
    allocated, in the merchant's favour, never invented: the charged total is
    always ``sum(unit * quantity)``, which is what the customer is billed.
    `price_kit` therefore REPORTS what this function allocated rather than what
    was asked for, so the kit's own arithmetic adds up: a discount of 1 cent
    that reached no line is reported as 0, not as 1 against an unchanged total.

    ponytail: the ceiling is that a merchant asking for a discount smaller than
    the member quantities can carry gets less than they typed. The upgrade path,
    if one ever notices, is a per-unit price_override plus a one-cent adjustment
    line, which is a worse cart to read than a discount one cent shy.
    """
    weights = [m.unit_list_cents * m.quantity for m in members]
    total = sum(weights)
    if total <= 0 or discount_cents <= 0:
        return tuple(0 for _ in members)

    units = []
    for member, weight in zip(members, weights, strict=True):
        # floor(exact line share / quantity), i.e. the largest whole unit-cent
        # that keeps this line at or under its exact share.
        units.append(discount_cents * weight // (total * member.quantity))

    remaining = discount_cents - sum(
        u * m.quantity for u, m in zip(units, members, strict=True)
    )
    # Shortfall against the exact share, kept as an integer numerator over the
    # common denominator `total`, so no float ever decides who gets a cent.
    shortfall = [
        discount_cents * weight - units[i] * members[i].quantity * total
        for i, weight in enumerate(weights)
    ]
    order = sorted(
        range(len(members)),
        key=lambda i: (-shortfall[i], -members[i].unit_list_cents, i),
    )
    progress = True
    while remaining > 0 and progress:
        progress = False
        for i in order:
            member = members[i]
            if member.quantity <= remaining and units[i] < member.unit_list_cents:
                units[i] += 1
                remaining -= member.quantity
                progress = True
                if remaining == 0:
                    break
    return tuple(units)


def price_kit(
    members,
    discount_kind: str,
    discount_amount,
    *,
    kit_quantity: int = 1,
    tier_lookup=None,
    user=None,
) -> KitPrice:
    """Price every member line, and the kit as a whole.

    `tier_lookup(variant, user, quantity) -> amount | None` is injected: wsm.dealer
    lands in its own unit and this app never imports it. None means retail.
    """
    members = list(members)
    if not members:
        raise KitRefusal("a kit with no members has no price")
    if kit_quantity < 1:
        raise KitRefusal("quantity must be at least 1")

    if any(m.quantity < 1 for m in members):
        # prorate divides by the member quantity: a zero reached this as a
        # ZeroDivisionError, which reads as a crash rather than as the bad kit
        # row it is. `KitMember.quantity` refuses it at the model now too.
        raise KitRefusal("a kit member holds fewer than one of its variant")

    list_total = sum(m.unit_list_cents * m.quantity for m in members)
    discount = kit_discount_cents(discount_kind, discount_amount, list_total)
    per_unit_discount = prorate(members, discount)
    # What the members actually took, which is what the kit discounted. Asking
    # for a cent that no member quantity can carry allocates nothing, and
    # reporting the ask would leave list_total - discount != total.
    allocated = sum(
        unit * member.quantity
        for unit, member in zip(per_unit_discount, members, strict=True)
    )

    lines = []
    for member, unit_discount in zip(members, per_unit_discount, strict=True):
        retail_unit = member.unit_list_cents - unit_discount
        line_quantity = member.quantity * kit_quantity
        unit, on_tier = retail_unit, False
        if tier_lookup is not None:
            tier = tier_lookup(member.variant, user, line_quantity)
            if tier is not None and to_cents(tier) < retail_unit:
                unit, on_tier = to_cents(tier), True
        share = str(
            (
                Decimal(member.unit_list_cents * member.quantity) / Decimal(list_total)
            ).quantize(_CENT, rounding=ROUND_HALF_UP)
        )
        lines.append(
            MemberPrice(
                member=member,
                unit_cents=unit,
                line_quantity=line_quantity,
                share=share,
                on_tier=on_tier,
            )
        )

    return KitPrice(
        lines=tuple(lines),
        list_total_cents=list_total * kit_quantity,
        discount_cents=allocated * kit_quantity,
        total_cents=sum(line.unit_cents * line.line_quantity for line in lines),
    )


def add_kit_to_checkout(
    checkout, kit, quantity, user=None, tier_lookup=None, tier_group=None
):
    """Explode a kit into ordinary checkout lines at prices computed right here.

    Returns `(group_id, KitPrice, {variant_id: CheckoutLine})`. The lines are
    ordinary: nothing downstream needs to know a kit made them, and a shopper who
    deletes one is left with the others at their own prices, which is exactly
    what the "kits are never a Saleor object beyond the Collection" ruling asks
    for.
    """
    # Imported here so the pure half of this module stays importable without a
    # configured Django, which is what makes the money tests cheap to run.
    from django.contrib.sites.models import Site

    from ...checkout.fetch import fetch_checkout_info, fetch_checkout_lines
    from ...checkout.utils import add_variants_to_checkout, invalidate_checkout
    from ...core.utils.metadata_manager import MetadataItem
    from ...graphql.checkout.mutations.utils import CheckoutLineData
    from ...plugins.manager import get_plugins_manager
    from ..dealer.no_stacking import LINE_METADATA_KEY as DEALER_KEY
    from ..dealer.no_stacking import PRICE_OVERRIDE_REASON as DEALER_REASON
    from ..checkout import check_addable

    priced = price_kit(
        kit.pricing_members(checkout.channel),
        kit.discount_kind,
        kit.discount_amount,
        kit_quantity=quantity,
        tier_lookup=tier_lookup,
        user=user,
    )

    group_id = str(uuid.uuid4())
    collection_slug = kit.collection.slug
    variants = []
    lines_data = []
    for line in priced.lines:
        variants.append(line.member.variant)
        metadata = [
            MetadataItem(META_GROUP, group_id),
            MetadataItem(
                META_KIT,
                json.dumps(
                    {"collection": collection_slug, "share": line.share},
                    sort_keys=True,
                ),
            ),
        ]
        reason = PRICE_OVERRIDE_REASON
        if line.on_tier:
            # A member line that took a tier IS a dealer line. The dealer stamp
            # that says so is written to PRIVATE metadata after the add, below:
            # it is a pricing input, and stock Saleor lets any unauthenticated
            # caller write PUBLIC line metadata.
            reason = DEALER_REASON
        lines_data.append(
            CheckoutLineData(
                variant_id=str(line.member.variant.pk),
                quantity=line.line_quantity,
                quantity_to_update=True,
                custom_price=Decimal(line.unit_cents) / 100,
                custom_price_to_update=True,
                custom_price_reason=reason,
                custom_price_reason_to_update=True,
                metadata_list=metadata,
            )
        )

    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    site_settings = Site.objects.get_current().settings
    # The checks `checkoutLinesAdd` runs before the identical write. A kit member
    # that went out of stock or off sale is refused as a whole: half a kit in the
    # cart is worse than none, and the money spec prices the members together.
    check_addable(
        checkout,
        checkout_info.channel,
        variants,
        lines_data,
        site_settings=site_settings,
        delivery_method_info=checkout_info.get_delivery_method_info(),
    )
    add_variants_to_checkout(
        checkout,
        variants,
        lines_data,
        checkout_info.channel,
        replace=False,
        replace_reservations=True,
        # Reservations are a stock feature the bake-off does not turn on.
        reservation_length=None,
        calculate_stocks_with_shipping_zones=(
            site_settings.use_legacy_shipping_zone_stock_availability
        ),
    )

    lines, _ = fetch_checkout_lines(checkout)
    checkout_info.lines = lines
    invalidate_checkout(checkout_info, lines, manager, save=True)

    ours = {
        info.line.variant_id: info.line
        for info in lines
        if info.line.metadata.get(META_GROUP) == group_id
    }

    from ...checkout.models import CheckoutLine

    stamped = []
    for line in priced.lines:
        row = ours.get(line.member.variant.pk)
        if row is None:
            continue
        # What MP3 re-derives this line from on every later recalculation. It is
        # PRIVATE because it is a pricing input: the public copy above is what
        # the storefront cart groups on and is display only, and stock Saleor
        # lets any unauthenticated caller write public line metadata. A shopper
        # who could write this one could hang a retail line off a heavily
        # discounted kit. The kit quantity is carried because the member line's
        # own quantity is the kit's times the member's, and dividing it back out
        # is a guess the moment a shopper edits it.
        row.store_value_in_private_metadata(
            {
                META_KIT: json.dumps(
                    {
                        "collection": collection_slug,
                        "group": group_id,
                        "quantity": quantity,
                    },
                    sort_keys=True,
                )
            }
        )
        stamped.append(row)
        if not line.on_tier:
            continue
        # A member line that took a tier IS a dealer line, and the no-stacking
        # guard finds one by the PRESENCE of this key
        # (dealer.no_stacking.is_dealer_line). Without it a catalogue promotion
        # or a SPECIFIC_PRODUCT voucher comes off the tier price and the dealer
        # collects both, which "better of, never both" is there to refuse. The
        # value carries the group for support; the break's own minimum quantity
        # is not carried, because a kit member is bought at the KIT's quantity
        # rather than at a rung the shopper chose.
        row.store_value_in_private_metadata(
            {DEALER_KEY: json.dumps({"group": tier_group} if tier_group else {})}
        )
    if stamped:
        CheckoutLine.objects.bulk_update(stamped, ["private_metadata"])

    return group_id, priced, ours
