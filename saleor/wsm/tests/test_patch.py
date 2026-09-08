# WSM-FORK: fork-owned file. See FORK-NOTES.md.
"""Tripwires for the app seam itself, not for what the feature computes.

The fork ships as a Django app that swaps one resolver at `ready()` time. Three
things can go wrong on an upstream rebase without any feature test noticing: the
swap can stop reaching the schema, the OFF path can stop being upstream's own
function, and the copied resolver body can go stale. One test each.
"""

import subprocess
import sys

import pytest

from ...graphql.api import schema
from .. import category_products

# The upstream commit the fork is branched from. `saleor/settings.py` is the only
# file outside `saleor/wsm/` the fork is allowed to have touched since.
UPSTREAM_BASE = "0a8ae002"


def test_the_patched_resolver_is_the_one_wired_into_the_schema():
    """A rebase that renames or moves `resolve_products` must FAIL here.

    `patch()` swaps a class attribute, and graphene reads that attribute once,
    when it builds the schema. If `ready()` ever lands after the build, or the
    attribute stops being what graphene reads, the swap becomes a silent no-op
    and every category page quietly loses the feature. So assert against the
    built schema, not against the class.
    """
    # given
    field = schema.get_type_map()["Category"].fields["products"]

    # when
    # Saleor's `FilterConnectionField` wraps the resolver it is given, so unwrap.
    chain = []
    resolver = field.resolver
    while resolver is not None:
        chain.append(resolver)
        resolver = getattr(resolver, "__wrapped__", None)

    # then
    assert category_products.resolve_products in chain


def test_the_feature_off_path_calls_upstreams_own_function(monkeypatch):
    """OFF is upstream by construction, not by re-implementation.

    The wrapper delegates to the function object it captured from upstream, so
    there is nothing to keep in sync and nothing to review for parity. This pins
    both halves: that the captured object really is upstream's resolver, and that
    it is what runs whenever the probe says no.
    """
    # given
    upstream = category_products._upstream_resolve_products
    assert upstream.__module__ == "saleor.graphql.product.types.categories"
    assert upstream.__qualname__ == "Category.resolve_products"

    calls = []

    def fake_upstream(root, info, *, channel=None, **kwargs):
        calls.append((root, channel, kwargs))
        return "upstream answered"

    monkeypatch.setattr(category_products, "_upstream_resolve_products", fake_upstream)
    monkeypatch.setattr(
        category_products,
        "secondary_categories_active",
        lambda database_connection_name: False,
    )

    class Info:
        context = type("Context", (), {"allow_replica": False})()

    # when
    result = category_products.resolve_products("root", Info(), channel="main", first=5)

    # then
    assert result == "upstream answered"
    assert calls == [("root", "main", {"first": 5})]


def test_the_upstream_resolver_source_is_unchanged():
    """The copied body is only correct against the upstream body it was ported from.

    `_resolve_products_with_secondary` is a line-for-line copy of upstream's
    resolver with two marked deviations. Nothing else can notice upstream editing
    that resolver, because the fork's copy keeps working and keeps being wrong.
    Re-pin deliberately: port the new body, then update the digest.
    """
    # given / when
    digest = category_products.upstream_source_digest()

    # then
    assert digest == category_products.UPSTREAM_RESOLVER_SHA256, (
        "saleor/graphql/product/types/categories.py's resolve_products changed. "
        "Re-port saleor/wsm/category_products.py's copy of its body, then update "
        "UPSTREAM_RESOLVER_SHA256."
    )


def test_patch_refuses_to_run_after_the_schema_was_imported(monkeypatch):
    """R9-1: the swap is a silent no-op if graphene already copied the resolver.

    Graphene reads `Category.resolve_products` once, at `saleor.graphql.api`
    import. If that module is in `sys.modules` before `patch()`, the class
    attribute changes and the built schema does not, so every category page
    would quietly serve primary-only at 200. `patch()` must refuse loudly.
    """
    # given a fresh patch state and a schema module already imported
    monkeypatch.setattr(category_products, "_upstream_resolve_products", None)
    monkeypatch.setitem(sys.modules, "saleor.graphql.api", object())

    # when / then
    with pytest.raises(RuntimeError, match="silent no-op"):
        category_products.patch()


def test_patch_refuses_to_stack_on_another_wrapper(monkeypatch):
    """A foreign `functools.wraps` wrapper must not pass as upstream.

    `functools.wraps` copies `__module__` and `__qualname__`, so a foreign
    wrapper can impersonate upstream to any name-based check. `__code__` is what
    wraps does not copy; `patch()` keys on it and refuses to stack.
    """
    import functools

    from ...graphql.product.types.categories import Category

    upstream = Category.resolve_products

    @functools.wraps(upstream)
    def foreign(*args, **kwargs):
        return upstream(*args, **kwargs)

    monkeypatch.setattr(category_products, "_upstream_resolve_products", None)
    monkeypatch.setattr(Category, "resolve_products", staticmethod(foreign))
    monkeypatch.delitem(sys.modules, "saleor.graphql.api", raising=False)

    with pytest.raises(RuntimeError, match="already wrapped"):
        category_products.patch()


def test_the_fork_touches_exactly_one_upstream_file():
    """`saleor/settings.py` plus `saleor/wsm/**`. Nothing else, ever.

    Bill and Matias both hold that we do not commit on top of saleor, so that is
    a property of the diff and belongs in a test. Diffed against the working tree
    rather than HEAD so an uncommitted stray edit fails too.
    """
    # given / when. Skip only when there is no git at all (a wheel, a tarball).
    # A git checkout that cannot see the base object is a shallow clone, and a
    # shallow clone silently skipping the one test that enforces the footprint
    # is worse than a failure: it fails loudly and says how to fix the checkout.
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", UPSTREAM_BASE, "--", "saleor/"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        pytest.skip(f"no git on this box: {error}")
    assert result.returncode == 0, (
        f"git cannot diff against {UPSTREAM_BASE} (exit {result.returncode}: "
        f"{result.stderr.strip()}). A shallow clone hides the upstream base; "
        "check out with fetch-depth: 0 so this footprint test can run."
    )
    changed = result.stdout.split()

    # then
    assert [path for path in changed if not path.startswith("saleor/wsm/")] == [
        "saleor/settings.py"
    ]
