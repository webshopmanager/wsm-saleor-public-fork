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

import importlib
import sys
from functools import wraps

from django.core.exceptions import ImproperlyConfigured


# Presence of this key on a checkout or order line means the line is priced at a
# dealer tier. The value carries the group and the break for support; nothing
# reads it back to price anything (design doc section 2, "presence only").
LINE_METADATA_KEY = "wsm.dealer"
PRICE_OVERRIDE_REASON = "wsm.dealer"

# Every module holding `attach_voucher_to_line_info` as its OWN attribute: the
# module that defines it, plus the two that imported it by name at import time.
# Patching the definer alone would leave those two on the original.
#
# `saleor.checkout.fetch` is deliberately absent. It imports the function inside
# the function that calls it, so it resolves through the definer at call time and
# has no attribute to rebind; patching the definer covers it.
#
# This is a PIN, not a hope: `install` discovers the real set and refuses to boot
# if it differs, so an upstream bump that adds an import site is a startup error
# rather than a checkout that silently stacks a voucher onto a dealer price.
VOUCHER_BINDING_SITES = (
    "saleor.discount.utils.voucher",
    "saleor.graphql.checkout.dataloaders.checkout_infos",
    "saleor.order.fetch",
)

_installed = False
_voucher_guard = None
_stacking: bool | None = None


def is_dealer_line(line) -> bool:
    return LINE_METADATA_KEY in (line.metadata or {})


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


def binding_sites(function) -> frozenset[str]:
    """Every loaded `saleor.` module whose attribute IS this exact function."""
    return frozenset(
        name
        for name, module in list(sys.modules.items())
        if name.startswith("saleor.")
        and getattr(module, "attach_voucher_to_line_info", None) is function
    )


def installed_voucher_guard():
    """The wrapper `install` put in place, for the test that pins the site set."""
    return _voucher_guard


def _guard_vouchers():
    global _voucher_guard

    # Import the pinned modules first: discovery can only see what is loaded, and
    # at app-ready time most of these have not been imported yet.
    for name in VOUCHER_BINDING_SITES:
        importlib.import_module(name)

    original = sys.modules["saleor.discount.utils.voucher"].attach_voucher_to_line_info
    discovered = binding_sites(original)
    if discovered != frozenset(VOUCHER_BINDING_SITES):
        raise ImproperlyConfigured(
            "wsm.dealer no-stacking: attach_voucher_to_line_info is bound in "
            f"{sorted(discovered)}, but this patch pins "
            f"{sorted(VOUCHER_BINDING_SITES)}. Rebind the new sites and update "
            "docs/wsm/CORE-TOUCHES.md, or a voucher will stack on a dealer price."
        )

    _voucher_guard = voucher_guard(original)
    for name in discovered:
        sys.modules[name].attach_voucher_to_line_info = _voucher_guard


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


def _guard_catalogue_promotions():
    from saleor.discount.utils import checkout as checkout_discounts

    checkout_discounts.prepare_checkout_line_discount_objects_for_catalogue_promotions = catalogue_guard(
        checkout_discounts.prepare_checkout_line_discount_objects_for_catalogue_promotions
    )


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
