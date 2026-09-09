# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""Requirement 2.4: no discount combines with dealer pricing, toggle default off.

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
_stacking: bool | None = None


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


def stacking_enabled() -> bool:
    """The toggle, cached per process.

    ponytail: process-local, so a merchant flipping the toggle needs the workers
    to cycle, or a second worker keeps stacking for its lifetime. Correct for one
    tenant per instance (design doc section 6). When this box serves more than
    one, move the cache to `django.core.cache` keyed by tenant and drop the
    signal below.
    """
    global _stacking
    if _stacking is None:
        from .models import DealerSettings

        _stacking = DealerSettings.stacking_enabled()
    return _stacking


def reset_cache(**_kwargs) -> None:
    global _stacking
    _stacking = None


def voucher_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def attach_voucher_to_line_info(voucher_info, lines_info):
        original(voucher_info, lines_info)
        dealer_lines = [info for info in lines_info if is_dealer_line(info.line)]
        if not dealer_lines or stacking_enabled():
            return
        for line_info in dealer_lines:
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
        dealer_lines = [info for info in lines_info if is_dealer_line(info.line)]
        if not dealer_lines or stacking_enabled():
            return original(lines_info)

        retail_lines = [info for info in lines_info if not is_dealer_line(info.line)]
        result = original(retail_lines)
        # A promotion that was already written onto a line before it became a
        # dealer line has to come off, or the toggle only works on new lines.
        stale = [
            discount
            for info in dealer_lines
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

    from django.db.models.signals import post_delete, post_save

    from .models import DealerSettings

    _guard_vouchers()
    _guard_catalogue_promotions()
    post_save.connect(reset_cache, sender=DealerSettings, dispatch_uid="wsm_dealer")
    post_delete.connect(reset_cache, sender=DealerSettings, dispatch_uid="wsm_dealer")
