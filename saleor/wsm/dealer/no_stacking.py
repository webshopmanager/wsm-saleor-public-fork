# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""Which lines a discount may touch, and the three wraps that hold the line.

Three rules share one traversal, `split_discountable`:

- Requirement 2.4: no discount combines with dealer pricing, toggle default off.
- A catalogue promotion is not taken twice off one line. A kit member line and a
  configured line are priced by the fork from the listing's SALE price
  (`money.unit_amount`), so the promotion is already inside the number written to
  `price_override`, and stock Saleor then takes the same rule off that override
  again. Measured live 2026-09-09: an Alba RZR900 kit member overridden at
  1899.99 under a 254.01 rule billed at 1645.98 and the kit at a subtotal of
  2278.98 instead of 2532.99. This one is the CATALOGUE path only; a voucher is
  a second discount the merchant does mean to stack on a sale price. It holds on
  the checkout AND on the order: `saleor/discount/utils/order.py` is a second
  copy of the same code, reached on every draft order recalculation and every
  order edit, and a rule that held in the cart and not on the order would quote
  a shopper the price they were not charged.
- A FEE line is never discountable, and there is no toggle on that one. A fee is
  a charge the merchant passes through (crating, core, environmental) carried as
  its own checkout line; in 5.0 a coupon came off the merchandise subtotal and
  never off a product fee. Without this rule an order-level voucher landed its
  WHOLE amount on the only line it was still allowed to reach, so a cart of one
  dealer-priced configured item plus its required crate charge took the entire
  100.00 off the charge and billed CRATE-01 at 49.00 instead of 149.00 (probe
  P10, 2026-09-08). A voucher over 149.00 would have zeroed a mandatory
  pass-through charge, and every retail cart carrying a fee was equally open.

There is no stock lever. Both discount paths were read before this was written:

- Vouchers on a checkout are not stored objects. `attach_voucher_to_line_info`
  decides which lines carry the voucher and `calculate_base_line_total_price`
  subtracts it from those. The only line the stock code ever excludes is a gift
  (`get_discounted_lines`, `is_gift`); there is no hook, setting or per-line flag.
- Catalogue promotions are explicitly designed to stack ON TOP of a price
  override: `saleor/discount/utils/promotion.py` lines 202-216 read
  `line.price_override` and apply the rule's discount to it.
- `manual_line_discount` DOES block a voucher on order lines, but only there,
  and buying the behaviour by writing a fake zero-value MANUAL discount onto
  every dealer line would put a discount row a merchant never created into the
  API, the order and the invoice.

So this is the bake-off's first monkey patch, and it is three wraps that all
drop undiscountable lines out of the line set the stock code discounts. Nothing
is reimplemented: the original functions do all the work, on fewer lines.

Cost: zero. A checkout with no dealer line takes the original path with no extra
call and no settings query; the toggle is read only once a dealer line is
present. That read is NOT cached, deliberately: `stacking_enabled` below says
why the per-process cache was removed. Measured at one query per guard fire, so
a dealer cart pays two per price recalculation.

Upstream change that would delete this file: a documented exclusion hook on the
checkout line-discount path, e.g. a `CheckoutLine.discounts_excluded` flag (or a
`can_discount_line(line_info)` predicate) honoured by both
`attach_voucher_to_line_info` and both
`prepare_*_line_discount_objects_for_catalogue_promotions`, the way `is_gift`
already is.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

from .. import patches

# The generic patch machinery lives with the pin it enforces. Re-exported here
# because this module was its first caller and its tests still name it.
from ..patches import binding_sites, install_guard  # noqa: F401

# Presence of this key on a checkout or order line means the line is priced at a
# dealer tier. The value carries the group and the break for support; nothing
# reads it back to price anything (design doc section 2, "presence only").
LINE_METADATA_KEY = "wsm.dealer"
PRICE_OVERRIDE_REASON = "wsm.dealer"

# Presence of this key means the line IS a charge: written as `compose.fee` by
# the configured-line add endpoint and rewritten by MP3 on every recalculation.
# The literal is repeated rather than imported because `saleor.wsm.compose.views`
# imports THIS module for the dealer key, and importing it back would be a cycle.
# `test_the_fee_marker_is_the_one_compose_writes` pins the two spellings together.
FEE_METADATA_KEY = "compose.fee"

# The `price_override_reason` values the fork writes on a line whose unit price
# it computed FROM the listing's sale price: `wsm.containers` on a kit member
# (containers/pricing.py) and `wsm.compose` on a configured line and its charge
# (compose/lines.py). Both literals are repeated here rather than imported for
# the reason FEE_METADATA_KEY is repeated: `saleor.wsm.compose.views` imports
# this module, so importing compose back would be a cycle, and containers is
# reached through compose. `test_the_sale_priced_reasons_are_the_ones_the_fork_writes`
# pins the four spellings together.
#
# `wsm.dealer` is deliberately absent. A dealer tier is a price beside the
# catalogue rather than one derived from it, so nothing is taken twice, and the
# dealer line already leaves the eligible set through `is_dealer_line` under the
# merchant's own toggle.
SALE_PRICED_REASONS = frozenset({"wsm.containers", "wsm.compose"})

# Every module holding `attach_voucher_to_line_info` as its OWN attribute, read
# from the one place the fork writes its patches down. Every patched function's
# sites live there, MP2's included, so there is one list to update on a rebase
# rather than one per monkey patch.
VOUCHER = "saleor.discount.utils.voucher.attach_voucher_to_line_info"
VOUCHER_BINDING_SITES = patches.PINNED[VOUCHER]
CATALOGUE = (
    "saleor.discount.utils.checkout."
    "prepare_checkout_line_discount_objects_for_catalogue_promotions"
)
CATALOGUE_BINDING_SITES = patches.PINNED[CATALOGUE]
ORDER_CATALOGUE = (
    "saleor.discount.utils.order."
    "prepare_order_line_discount_objects_for_catalogue_promotions"
)
ORDER_CATALOGUE_BINDING_SITES = patches.PINNED[ORDER_CATALOGUE]

_installed = False
_voucher_guard = None
_catalogue_guard = None
_order_catalogue_guard = None


def is_dealer_line(line) -> bool:
    """PRIVATE metadata only, on a checkout line and on an order line alike.

    Stock Saleor maps CheckoutLine PUBLIC metadata to `no_permissions`
    (saleor/graphql/meta/permissions.py), so reading the public copy would let
    a shopper mark their own retail line a dealer line and, with the guard
    inverted, let them un-mark a real one to stack a voucher on a tier price.
    `create_order_from_checkout` copies private metadata onto the order line, so
    MP2 still finds the stamp after completion.
    """
    return LINE_METADATA_KEY in (line.private_metadata or {})


def is_fee_line(line) -> bool:
    """PRIVATE metadata only, for the same reason `is_dealer_line` reads private.

    The public copy exists because the storefront cart pairs a charge to its
    parent off it, and any shopper can write it on their own line. Reading it
    here would let a shopper stamp `compose.fee` onto a merchandise line and
    make it undiscountable, which is their money and not an attack, but it is
    still a number decided by the caller. MP3 writes the private copy from the
    Fee row, and `create_order_from_checkout` carries it onto the order line.
    """
    return FEE_METADATA_KEY in (line.private_metadata or {})


def is_sale_priced_line(line) -> bool:
    """The COLUMN the fork stamped, not metadata, on a checkout or order line.

    `price_override_reason` is written beside `price_override` by the endpoints
    that set the price and rewritten by MP3 on every recalculation; there is no
    metadata mutation that reaches it, so unlike the public `wsm.kit` copy a
    shopper cannot stamp their own line with it. The two travel together: a
    reason without an override prices nothing, and the guard only matters where
    an override exists.
    """
    return line.price_override_reason in SALE_PRICED_REASONS


def split_discountable(objs, line_of=lambda obj: obj, also_excluded=None):
    """(eligible, excluded) for the discount paths. One pass, no query.

    `line_of` reaches the line, so the same function serves the LineInfo lists
    the checkout paths carry and the bare OrderLine lists the order paths carry.

    A fee line is always excluded. A dealer line is excluded unless the merchant
    turned stacking on, and that toggle is read at most once per call and only
    once a dealer line has actually been seen, so a cart of retail lines and
    charges still costs no settings query. Order is preserved within each list.

    Excluding a fee needs no toggle of its own: "discount my crating charge" has
    no merchant reading that a discount on the merchandise line cannot express,
    and the charge is money owed to somebody else.

    `also_excluded` is one more predicate on the line, for a rule that holds on
    ONE of the discount paths rather than both. The catalogue guard passes
    `is_sale_priced_line`; the voucher guards pass nothing, because a voucher is
    a discount the merchant means to give on top of a sale price. It is an
    argument rather than a third clause in the loop so that the caller, and the
    caller alone, says which rule it is under.
    """
    eligible: list[Any] = []
    excluded: list[Any] = []
    dealer_excluded = None
    for obj in objs:
        line = line_of(obj)
        if is_fee_line(line) or (also_excluded and also_excluded(line)):
            excluded.append(obj)
        elif is_dealer_line(line):
            if dealer_excluded is None:
                dealer_excluded = not stacking_enabled()
            (excluded if dealer_excluded else eligible).append(obj)
        else:
            eligible.append(obj)
    return eligible, excluded


def line_of_info(obj):
    """`line_of` for the LineInfo lists. A named function, not a lambda per call."""
    return obj.line


def stacking_enabled() -> bool:
    """The toggle, read from its one row, once per guard that fires.

    ONE indexed single-row query, and only on a checkout that has a dealer line,
    because every caller checks that first and returns before asking. A retail
    cart still costs zero.

    It was a process global with a `post_save` signal to clear it. A signal only
    reaches the process it fires in: gunicorn runs several workers and a celery
    worker prices too, so a merchant turning stacking off in the console left
    every OTHER worker stacking a discount onto dealer prices for the rest of its
    life, and which price a shopper got depended on which worker took the
    request. The signal also missed `.update()` and `bulk_update()` entirely.
    One query is cheaper than a wrong price.
    """
    from .models import DealerSettings

    return DealerSettings.stacking_enabled()


def voucher_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def attach_voucher_to_line_info(voucher_info, lines_info):
        original(voucher_info, lines_info)
        _eligible, excluded = split_discountable(lines_info, line_of_info)
        for line_info in excluded:
            line_info.voucher = None
            line_info.voucher_code = None

    return attach_voucher_to_line_info


def installed_voucher_guard():
    """The wrapper `install` put in place, for the test that pins the site set."""
    return _voucher_guard


def _guard_vouchers():
    global _voucher_guard
    _voucher_guard = install_guard(VOUCHER, voucher_guard)


def _catalogue_split(lines_info):
    """(eligible, excluded, stale) for either catalogue path. One pass, no query.

    `eligible` is the lines the original may still discount; `stale` is every
    catalogue row already sitting on a line that is no longer one of them. A
    merchant can put a product on sale after the line was created, so a rule
    that only skipped NEW lines would leave the old ones discounted twice.

    Decided here and not in each wrapper because a shopper meets both paths in
    one purchase: a line the cart refuses to discount and the order agrees to
    quotes two prices for the same thing, and the order's is the one billed.
    """
    eligible, excluded = split_discountable(
        lines_info, line_of_info, is_sale_priced_line
    )
    stale = [
        discount for info in excluded for discount in info.get_catalogue_discounts()
    ]
    return eligible, excluded, stale


def catalogue_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def prepare_checkout_line_discount_objects_for_catalogue_promotions(lines_info):
        eligible, excluded, stale = _catalogue_split(lines_info)
        if not excluded:
            return original(lines_info)

        result = original(eligible)
        if result is None:
            # The original answers nothing when it is handed no lines at all,
            # which is what a cart of nothing but kit members leaves it. The
            # stale rows still have to come off. The fifth slot is the promotion
            # end date, which no removal moves.
            return ([], [], stale, [], None) if stale else None
        # Slot 2 is `line_discounts_to_remove`, in both paths' answer.
        result[2].extend(stale)
        return result

    return prepare_checkout_line_discount_objects_for_catalogue_promotions


def order_catalogue_guard(original):
    """The same rule on the order path. A wrapper a test can build its own of.

    `saleor/discount/utils/order.py` is a second copy of the checkout catalogue
    code, reached on every draft order recalculation and every order edit, and
    it was unguarded: the kit member the cart charged 1899.99 fell to 1645.98
    the moment the checkout became an order. `create_order_from_checkout` copies
    `price_override_reason` and the private stamps onto the order line, so the
    same three predicates answer the question with no new field.

    It differs from the checkout wrapper in one thing only, the width of the
    answer: the order path has no promotion end date to carry.
    """

    @wraps(original)
    def prepare_order_line_discount_objects_for_catalogue_promotions(lines_info):
        eligible, excluded, stale = _catalogue_split(lines_info)
        if not excluded:
            return original(lines_info)

        result = original(eligible)
        if result is None:
            return ([], [], stale, []) if stale else None
        result[2].extend(stale)
        return result

    return prepare_order_line_discount_objects_for_catalogue_promotions


def installed_catalogue_guard():
    """The wrapper `install` put in place, for the test that pins the site set."""
    return _catalogue_guard


def installed_order_catalogue_guard():
    """The wrapper `install` put in place, for the test that pins the site set."""
    return _order_catalogue_guard


def _guard_catalogue_promotions():
    global _catalogue_guard
    _catalogue_guard = install_guard(CATALOGUE, catalogue_guard)


def _guard_order_catalogue_promotions():
    global _order_catalogue_guard
    _order_catalogue_guard = install_guard(ORDER_CATALOGUE, order_catalogue_guard)


def install() -> None:
    """Called once from DealerConfig.ready()."""
    global _installed
    if _installed:
        return
    _installed = True

    _guard_vouchers()
    _guard_catalogue_promotions()
    _guard_order_catalogue_promotions()
