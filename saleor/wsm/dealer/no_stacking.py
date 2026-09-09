# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""Which lines a discount may touch, and the two wraps that hold the line.

Two rules share one traversal, `split_discountable`:

- Requirement 2.4: no discount combines with dealer pricing, toggle default off.
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

So this is the bake-off's first monkey patch, and it is two wraps that both drop
dealer lines out of the line set the stock code discounts. Nothing is
reimplemented: the original functions do all the work, on fewer lines.

Cost: zero. A checkout with no dealer line takes the original path with no extra
call and no settings query; the toggle is read only once a dealer line is
present, and cached per process until the row changes.

Upstream change that would delete this file: a documented exclusion hook on the
checkout line-discount path, e.g. a `CheckoutLine.discounts_excluded` flag (or a
`can_discount_line(line_info)` predicate) honoured by both
`attach_voucher_to_line_info` and
`prepare_checkout_line_discount_objects_for_catalogue_promotions`, the way
`is_gift` already is.
"""

from __future__ import annotations

from functools import wraps

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

_installed = False
_voucher_guard = None
_catalogue_guard = None


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


def split_discountable(objs, line_of=lambda obj: obj):
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
    """
    eligible, excluded = [], []
    dealer_excluded = None
    for obj in objs:
        line = line_of(obj)
        if is_fee_line(line):
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


def catalogue_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def prepare_checkout_line_discount_objects_for_catalogue_promotions(lines_info):
        eligible, excluded = split_discountable(lines_info, line_of_info)
        if not excluded:
            return original(lines_info)

        result = original(eligible)
        # A promotion already written onto a line before it became undiscountable
        # has to come off, or the rule only holds for lines added after it.
        stale = [
            discount
            for info in excluded
            for discount in info.get_catalogue_discounts()
        ]
        if result is None:
            return ([], [], stale, [], None) if stale else None
        creates, updates, removes, updated_fields, end_date = result
        removes.extend(stale)
        return creates, updates, removes, updated_fields, end_date

    return prepare_checkout_line_discount_objects_for_catalogue_promotions


def installed_catalogue_guard():
    """The wrapper `install` put in place, for the test that pins the site set."""
    return _catalogue_guard


def _guard_catalogue_promotions():
    global _catalogue_guard
    _catalogue_guard = install_guard(CATALOGUE, catalogue_guard)


def install() -> None:
    """Called once from DealerConfig.ready()."""
    global _installed
    if _installed:
        return
    _installed = True

    _guard_vouchers()
    _guard_catalogue_promotions()
