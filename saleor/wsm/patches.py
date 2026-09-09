# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one list of core functions this fork wraps, plus a live reading of it.

A monkey patch is the fork's most expensive kind of change, because nothing in
the core file it changes says so. Every wrapped function is therefore named
here, once, and `saleor/wsm/tests/test_core_tables_untouched.py` compares this
tuple against what is ACTUALLY installed in the running process, discovered by
sweeping `sys.modules` rather than trusting a registration call. A patch that
lands without its line here reddens that test.

An entry is the DEFINING module and qualname of the wrapped function, so a patch
that rebinds one function in several modules is one entry, not one per site. Its
value is every module that holds that function as an attribute, the definer
included: `import` binds a name, so patching the definer alone leaves every
module that imported the function by name still calling the original.

Those tuples are a PIN, not a hope. `install_guard` DISCOVERS the real set at
startup and refuses to boot if it differs, so an upstream bump that adds an
import site is a startup error rather than a checkout that silently stacks a
discount onto a dealer price.
"""

import importlib
import sys
import types

from django.core.exceptions import ImproperlyConfigured

PINNED = {
    # MP1, saleor/wsm/dealer/no_stacking.py: no voucher and no catalogue
    # promotion stacks on a dealer-priced line.
    #
    # `saleor.checkout.fetch` is deliberately absent from the voucher sites. It
    # imports the function inside the function that calls it, so it resolves
    # through the definer at call time and has no attribute to rebind.
    "saleor.discount.utils.voucher.attach_voucher_to_line_info": (
        "saleor.discount.utils.voucher",
        "saleor.graphql.checkout.dataloaders.checkout_infos",
        "saleor.order.fetch",
    ),
    "saleor.discount.utils.checkout."
    "prepare_checkout_line_discount_objects_for_catalogue_promotions": (
        "saleor.discount.utils.checkout",
    ),
    # MP2, saleor/wsm/dealer/no_stacking_order_level.py: an order-level discount
    # is computed and spread over the retail lines only.
    "saleor.checkout.utils.get_voucher_discount_for_checkout": (
        "saleor.checkout.utils",
    ),
    "saleor.checkout.base_calculations."
    "_propagate_checkout_discount_on_checkout_lines_prices": (
        "saleor.checkout.base_calculations",
    ),
    "saleor.order.base_calculations.propagate_order_discount_on_order_prices": (
        "saleor.order.base_calculations",
        "saleor.plugins.manager",
    ),
    "saleor.order.base_calculations."
    "propagate_order_discount_on_order_lines_prices": (
        "saleor.order.base_calculations",
    ),
    "saleor.discount.utils.promotion."
    "create_discount_objects_for_order_promotions": (
        "saleor.discount.utils.promotion",
        "saleor.discount.utils.checkout",
        "saleor.discount.utils.order",
    ),
    # MP3, saleor/wsm/reprice.py: every price this fork owns is re-derived
    # from our own tables and the CURRENT line quantities on every price
    # recalculation, so no stored override can outlive the facts it was
    # computed from.
    "saleor.checkout.calculations._fetch_checkout_prices_if_expired": (
        "saleor.checkout.calculations",
    ),
}


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
            # `isinstance`, not a None check: a test double left in a core
            # module namespace answers EVERY attribute, so it hands back a
            # stand-in for `__code__` too and `co_filename` then raises. Only a
            # real code object can say where a function was defined.
            if original is None or not isinstance(code, types.CodeType):
                continue
            if "/saleor/wsm/" in code.co_filename.replace("\\", "/"):
                found.add(f"{original.__module__}.{original.__qualname__}")
    return frozenset(found)


def binding_sites(function) -> frozenset[str]:
    """Every loaded `saleor.` module holding this exact function as an attribute.

    By identity over every attribute, not by name, so `import ... as` is found
    too. One pass over `sys.modules` per patched function, at `ready()` only.
    The fork's own modules are skipped: they hold the WRAPPER, which is what this
    function is asked about, and a site here is by definition a core one.
    """
    return frozenset(
        name
        for name, module in list(sys.modules.items())
        if name.startswith("saleor.")
        and not name.startswith("saleor.wsm")
        and any(
            value is function
            for value in list(getattr(module, "__dict__", {}).values())
        )
    )


def install_guard(name, guard):
    """Wrap the core function `name` and rebind it at every site that holds it.

    `name` is its key in `PINNED` above: the defining module and
    qualname. The sites are pinned there and DISCOVERED here, and a discovered
    set that differs from the pinned one is a boot error rather than a checkout
    that silently prices a line the way the storefront last asked for.
    """
    pinned = PINNED.get(name)
    if pinned is None:
        raise ImproperlyConfigured(
            f"{name} is patched but not named in "
            "saleor/wsm/patches.py PINNED. Add it there and to "
            "docs/wsm/CORE-TOUCHES.md."
        )
    # Discovery can only see what is loaded, and at app-ready time most of these
    # have not been imported yet.
    for site in pinned:
        importlib.import_module(site)

    definer, _, attribute = name.rpartition(".")
    original = getattr(sys.modules[definer], attribute)
    discovered = binding_sites(original)
    if discovered != frozenset(pinned):
        raise ImproperlyConfigured(
            f"{name} is bound in {sorted(discovered)}, "
            f"but this patch pins {sorted(pinned)}. Rebind the new sites and "
            "update docs/wsm/CORE-TOUCHES.md, or the wrapper stands in for "
            "the function at some call sites and not at others."
        )

    guarded = guard(original)
    for site in discovered:
        module = sys.modules[site]
        for attr, value in list(vars(module).items()):
            if value is original:
                setattr(module, attr, guarded)
    return guarded
