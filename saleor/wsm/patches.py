# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one list of core functions this fork wraps, plus a live reading of it.

A monkey patch is the fork's most expensive kind of change, because nothing in
the core file it changes says so. Every wrapped function is therefore named
here, once, and `saleor/wsm/tests/test_core_tables_untouched.py` compares this
tuple against what is ACTUALLY installed in the running process, discovered by
sweeping `sys.modules` rather than trusting a registration call. A patch that
lands without its line here reddens that test.

An entry is the DEFINING module and qualname of the wrapped function, so a patch
that rebinds one function in several modules is one entry, not one per site.
"""

import sys

PINNED = (
    # MP1, saleor/wsm/dealer/no_stacking.py: no voucher and no catalogue
    # promotion stacks on a dealer-priced line.
    "saleor.discount.utils.voucher.attach_voucher_to_line_info",
    "saleor.discount.utils.checkout."
    "prepare_checkout_line_discount_objects_for_catalogue_promotions",
    # MP2, saleor/wsm/dealer/no_stacking_order_level.py: an order-level discount
    # is computed and spread over the retail lines only.
    "saleor.checkout.utils.get_voucher_discount_for_checkout",
    "saleor.checkout.base_calculations."
    "_propagate_checkout_discount_on_checkout_lines_prices",
    "saleor.order.base_calculations.propagate_order_discount_on_order_prices",
    "saleor.order.base_calculations.propagate_order_discount_on_order_lines_prices",
    "saleor.discount.utils.promotion.create_discount_objects_for_order_promotions",
)


def installed() -> frozenset[str]:
    """Every core function a `saleor/wsm/` wrapper is currently standing in for.

    `functools.wraps` copies `__module__` from the original onto the wrapper, so
    the wrapper's own module is not evidence. Its code object is: a wrapper
    defined under `saleor/wsm/` and bound as an attribute of a core module is a
    monkey patch, whatever it calls itself.
    """
    found = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("saleor.") or name.startswith("saleor.wsm"):
            continue
        for value in list(vars(module).values()):
            original = getattr(value, "__wrapped__", None)
            code = getattr(value, "__code__", None)
            if original is None or code is None:
                continue
            if "/saleor/wsm/" in code.co_filename.replace("\\", "/"):
                found.add(f"{original.__module__}.{original.__qualname__}")
    return frozenset(found)
