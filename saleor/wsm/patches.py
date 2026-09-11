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

`SOURCE` below is the other half of the same idea. `PINNED` says WHERE a
function is bound; a binding-site sweep cannot see that its BODY changed. MP3
re-derives prices around `_fetch_checkout_prices_if_expired`, and MP1 and MP2
wrap functions whose internals decide how a discount is split, so an upstream
release that rewrites one of those bodies without moving an import passes the
site guard and silently invalidates the assumption the wrapper was written on.
This fork has been bitten by exactly that class once already (the
catalogue-promotion double-take fixed at 08b221ec). A digest per patch turns it
into a startup error.

`EXTENDED` is the fourth ledger, for the fourth thing. MP4
(`saleor/wsm/graphql/compose/product_extension.py`) does not wrap a function: it
APPENDS fields to stock's `Product` graphene type, which `installed()` cannot
see because there is no `__wrapped__` anywhere. It is still a mutation of a core
class from a file that class never mentions, which is the expensive half of a
monkey patch, so it gets the same treatment: the real set is discovered off the
core types by `extensions_installed()` and compared against the pin below.
"""

import hashlib
import importlib
import inspect
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
    # The order path's own copy of the line above. `refresh_order_...` reaches
    # it through the module global in the file that defines it, so the definer
    # is the only site; the sweep in `install_guard` says so at every boot
    # rather than this comment saying so once.
    "saleor.discount.utils.order."
    "prepare_order_line_discount_objects_for_catalogue_promotions": (
        "saleor.discount.utils.order",
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
    "saleor.order.base_calculations.propagate_order_discount_on_order_lines_prices": (
        "saleor.order.base_calculations",
    ),
    "saleor.discount.utils.promotion.create_discount_objects_for_order_promotions": (
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

# MP4, saleor/wsm/graphql/compose/product_extension.py: the product page hosts
# a Compose tab, so stock's `Product` type carries three more fields. Additive,
# idempotent and ordered (see that module's docstring for why it is not a core
# edit and what it costs); ledgered here because it changes a class defined in
# `saleor/graphql/product/types/products.py` from outside that file.
# Value: the field names, as `Product._meta.fields` keys.
EXTENDED = {
    "saleor.graphql.product.types.products.Product": (
        "wsm_compliance",
        "wsm_fees",
        "wsm_option_sets",
    ),
}

# MP5, saleor/wsm/graphql/cost.py: one module ATTRIBUTE repointed, so the query
# complexity guard weighs the schema this fork serves instead of the stock
# object `saleor/graphql/views.py:38` imports. Stock parses with `self.schema`
# and costs with the module global; on stock Saleor they are the same object, on
# this fork they are not, and the difference is a surface with no ceiling.
# Discovered by the `_wsm_owned` marker the fork puts on the object it binds.
REBOUND = frozenset({"saleor.graphql.views.schema"})


SOURCE = {
    "saleor.discount.utils.voucher.attach_voucher_to_line_info": (
        "d0f6e09a314d8a43b90d76ae131668f4b37ee4f00ba51f6b9b4b3d0ceada36a4"
    ),
    "saleor.discount.utils.checkout.prepare_checkout_line_discount_objects_for_catalogue_promotions": (
        "921f54e84a37b21a75a84dd5a38861393a63ab004e8907d37e35310ee96b6667"
    ),
    "saleor.discount.utils.order.prepare_order_line_discount_objects_for_catalogue_promotions": (
        "c8a109dd23adcc4668a06d2404403b46e6a703c04d63b44f89b5dd18e3a2b254"
    ),
    "saleor.checkout.utils.get_voucher_discount_for_checkout": (
        "5f5df5601cf559f882de793a5bf56b025c6e367e6f578b408c53bb736f5d3429"
    ),
    "saleor.checkout.base_calculations._propagate_checkout_discount_on_checkout_lines_prices": (
        "0bde6a4556a72e95649e43157a32418cf93d8a304cc8c1c2cf8915763b25860d"
    ),
    "saleor.order.base_calculations.propagate_order_discount_on_order_prices": (
        "732bf6eb0ec74a6966086f109b88c6311fd4a200760091b8f97513d68278a029"
    ),
    "saleor.order.base_calculations.propagate_order_discount_on_order_lines_prices": (
        "d7de3c1173fea60ad5a246925ec69d0831b3647637ae1bddfeb80ef49f0aff0d"
    ),
    "saleor.discount.utils.promotion.create_discount_objects_for_order_promotions": (
        "bf3f44ceb097e67a0661b8057a34ebbaabea7feb53fde484b51ad28be52543af"
    ),
    "saleor.checkout.calculations._fetch_checkout_prices_if_expired": (
        "e2d2731d30d0a8adf66587846aaf4e3f1578fce65f080fcfa6865f0a31c5c131"
    ),
}


def source_digest(function) -> str:
    """The sha256 of a function's own source, as `inspect` reads it.

    `inspect.getsourcelines` unwraps, so this answers about the ORIGINAL even
    when handed our wrapper. That is what lets the suite check the digests long
    after boot, when every name in PINNED already resolves to a wrapper.

    To re-pin after a deliberate upstream bump, read the upstream diff FIRST,
    then print the new digests with the one-liner in docs/wsm/CORE-TOUCHES.md.
    """
    return hashlib.sha256(inspect.getsource(function).encode()).hexdigest()


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


def extensions_installed() -> dict[str, tuple[str, ...]]:
    """Every core graphene type this fork has appended fields to, discovered.

    Graphene marks nothing, so the fork does: `product_extension` sets
    `_wsm_owned` on each field it appends, and this sweeps the loaded core
    modules for any type whose `_meta.fields` carries one. Discovery rather than
    a registration call, for the same reason `installed()` sweeps rather than
    trusts: a patch that lands without telling anyone is exactly the one this
    ledger exists to catch.
    """
    found: dict[str, tuple[str, ...]] = {}
    seen: set[int] = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("saleor.") or name.startswith("saleor.wsm"):
            continue
        for value in list(vars(module).values()):
            if not isinstance(value, type) or id(value) in seen:
                continue
            fields = getattr(getattr(value, "_meta", None), "fields", None)
            # A Django model's `_meta.fields` is a tuple, not a mapping; only a
            # graphene type answers this shape.
            if not isinstance(fields, dict):
                continue
            seen.add(id(value))
            owned = tuple(
                sorted(
                    field
                    for field, declaration in fields.items()
                    if getattr(declaration, "_wsm_owned", False) is True
                )
            )
            if owned:
                found[f"{value.__module__}.{value.__qualname__}"] = owned
    return found


def rebindings_installed() -> frozenset[str]:
    """Every core module attribute now holding an object this fork put there.

    Same discipline as `installed()` and `extensions_installed()`: sweep, do not
    trust a registration call. The fork marks what it binds with `_wsm_owned`,
    so this finds the binding wherever it landed rather than wherever it was
    meant to land.
    """
    found = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("saleor.") or name.startswith("saleor.wsm"):
            continue
        for attribute, value in list(getattr(module, "__dict__", {}).items()):
            # `getattr` with a default, because a mock left in a core namespace
            # answers every attribute and some of those answers raise.
            try:
                owned = getattr(value, "_wsm_owned", False) is True
            except Exception:  # pragma: no cover - a hostile test double
                continue
            # A graphene FIELD carrying the marker is MP4's business
            # (`extensions_installed`), not a module-level rebind.
            if owned and not isinstance(value, type):
                found.add(f"{name}.{attribute}")
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

    digest = SOURCE.get(name)
    if digest is None:
        raise ImproperlyConfigured(
            f"{name} is patched but has no source digest in "
            "saleor/wsm/patches.py SOURCE. Add one, so an upstream bump that "
            "rewrites the body this wrapper was written around is a boot error."
        )
    found = source_digest(original)
    if found != digest:
        raise ImproperlyConfigured(
            f"{name} is not the function this patch was written against: "
            f"pinned {digest}, found {found}. Read the upstream diff for that "
            "function, decide whether the wrapper still holds, then re-pin it "
            "in saleor/wsm/patches.py SOURCE and say so in "
            "docs/wsm/CORE-TOUCHES.md."
        )

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
