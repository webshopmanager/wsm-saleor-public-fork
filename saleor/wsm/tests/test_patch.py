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
from .test_core_tables_untouched import ALLOWED_CORE_FILES, REPO

# The upstream release the fork sits on. The base commit is DERIVED from it
# rather than pinned, because a rebase moves the base and a pinned sha then
# fails for a reason that has nothing to do with the footprint it guards.
UPSTREAM_TAG = "3.23.31"


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


def test_the_fork_touches_exactly_the_allowed_upstream_files():
    """`saleor/wsm/**` plus the deviation budget, and nothing else, ever.

    Bill and Matias both hold that we do not commit on top of saleor, so that is
    a property of the diff and belongs in a test. The allowed set is IMPORTED
    from the guard test rather than restated here, so the budget has one home and
    the two tests cannot drift apart. Diffed against the working tree rather than
    HEAD so an uncommitted stray edit fails too.
    """
    # given / when. Skip only when there is no git at all (a wheel, a tarball).
    # A git checkout that cannot see the base object is a shallow clone, and a
    # shallow clone silently skipping the one test that enforces the footprint
    # is worse than a failure: it fails loudly and says how to fix the checkout.
    def git(*args):
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO, capture_output=True, text=True
            )
        except FileNotFoundError as error:
            pytest.skip(f"no git on this box: {error}")

    base = git("merge-base", "HEAD", UPSTREAM_TAG)
    assert base.returncode == 0, (
        f"git cannot find the merge base with {UPSTREAM_TAG} (exit "
        f"{base.returncode}: {base.stderr.strip()}). A shallow clone hides the "
        "upstream base; check out with fetch-depth: 0 so this footprint test can "
        "run."
    )
    result = git("diff", "--name-only", base.stdout.strip(), "--", "saleor/")
    assert result.returncode == 0, (
        f"git cannot diff against {base.stdout.strip()} (exit "
        f"{result.returncode}: {result.stderr.strip()})."
    )
    changed = result.stdout.split()

    # then
    assert {
        path for path in changed if not path.startswith("saleor/wsm/")
    } == ALLOWED_CORE_FILES


# --- the OTHER half of the pin: what the wrapped body looked like ----------


def test_every_patched_function_still_has_the_body_its_wrapper_was_written_around():
    """A body that moved under a wrapper is the failure the site sweep cannot see.

    `PINNED` says where each function is bound, and `install_guard` refuses to
    boot when that set drifts. It says nothing about the function's contents:
    MP3 re-derives prices around `_fetch_checkout_prices_if_expired`, MP1 and MP2
    wrap functions whose internals decide how a discount is split, and an
    upstream release can rewrite any of those bodies without moving an import.
    """
    import importlib

    from .. import patches

    for name, digest in patches.SOURCE.items():
        definer, _, attribute = name.rpartition(".")
        function = getattr(importlib.import_module(definer), attribute)
        assert patches.source_digest(function) == digest, (
            f"{name} is not the function the wrapper was written around. Read "
            "the upstream diff for it, decide whether the wrapper still holds, "
            "then re-pin the digest."
        )


def test_every_pinned_patch_carries_a_source_digest():
    from .. import patches

    assert set(patches.SOURCE) == set(patches.PINNED)


def test_a_body_that_moved_is_a_boot_error_not_a_mispriced_checkout(monkeypatch):
    import pytest as _pytest
    from django.core.exceptions import ImproperlyConfigured

    from .. import patches

    name = "saleor.checkout.calculations._fetch_checkout_prices_if_expired"
    monkeypatch.setitem(patches.SOURCE, name, "0" * 64)

    with _pytest.raises(ImproperlyConfigured, match="written against"):
        patches.install_guard(name, lambda original: original)


def test_a_patch_with_no_digest_at_all_is_a_boot_error(monkeypatch):
    import pytest as _pytest
    from django.core.exceptions import ImproperlyConfigured

    from .. import patches

    name = "saleor.checkout.calculations._fetch_checkout_prices_if_expired"
    monkeypatch.delitem(patches.SOURCE, name)

    with _pytest.raises(ImproperlyConfigured, match="no source digest"):
        patches.install_guard(name, lambda original: original)
