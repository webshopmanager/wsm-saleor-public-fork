"""WSM-FORK: fork-owned file. Wire the secondary-category union into the schema.

`WsmConfig.ready()` calls `patch()`, which replaces `Category.resolve_products`
with `resolve_products` below BEFORE graphene builds the schema, so the fork ships
as an app and `saleor/graphql/` stays byte-identical to upstream.

**Feature OFF calls upstream's own function object**, not a re-implementation of
it, so the off path is identical by construction rather than by review. Only the
ON path runs the copy of upstream's body below, and that copy is pinned by
`tests/test_patch.py::test_the_upstream_resolver_source_is_unchanged`, which fails
on an upstream rebase that edits the resolver. See FORK-NOTES.md hazard 1.

Imported only from `ready()`, which is why importing `saleor.graphql` at module
level here is safe: every model is loaded by then, and nothing under
`saleor/graphql/` imports `saleor.wsm`, so the dependency runs one way.
"""

import hashlib
import inspect
import sys

from ..core.search import prefix_search
from ..graphql.channel.dataloaders.by_self import ChannelBySlugLoader
from ..graphql.channel.utils import get_default_channel_slug_or_graphql_error
from ..graphql.core.connection import (
    create_connection_slice,
    filter_connection_queryset,
)
from ..graphql.core.context import ChannelQsContext, get_database_connection_name
from ..graphql.core.utils import validate_and_apply_search_rank_sorting
from ..graphql.product.sorters import ProductOrderField
from ..graphql.product.types.categories import Category
from ..graphql.product.types.products import ProductCountableConnection
from ..graphql.utils import get_user_or_app_from_context
from ..permission.utils import has_one_of_permissions
from ..product import models
from ..product.models import ALL_PRODUCTS_PERMISSIONS
from .secondary_categories import secondary_categories_active, with_secondary_categories

# The sha256 of `inspect.getsource(_upstream_resolve_products)` as of the 3.22 tag.
# `_resolve_products_with_secondary` below is a line-for-line copy of that body with
# two documented deviations; if upstream edits the resolver this stops matching and
# the tripwire test fails, which is the only thing standing between a rebase and a
# silently stale copy. To re-pin: read the new upstream body, port the two
# deviations, then update this digest.
UPSTREAM_RESOLVER_SHA256 = (
    "00cc4e80bf42b21e50e9c1573a10c00dec39e331ce1c8e5ee76f1ea9e871b7f8"
)

_upstream_resolve_products = None


def upstream_source_digest() -> str:
    """Return the sha256 of the upstream resolver the copy below was ported from."""
    source = inspect.getsource(_upstream_resolve_products)  # type: ignore[arg-type]
    return hashlib.sha256(source.encode()).hexdigest()


def patch() -> None:
    """Swap `Category.resolve_products` for the wrapper. Idempotent.

    Two loud failures instead of two silent ones. Graphene copies the resolver
    off the class exactly once, when `saleor.graphql.api` builds the TypeMap at
    import, so if anything imported that module before this app's `ready()`
    (a `PLUGINS` entry that imports the schema at module level, an app listed
    earlier that does the same) the swap would land on a class nobody reads
    again and every category page would quietly revert to primary-only at 200.
    And if another app already wrapped the resolver with `functools.wraps`, the
    "upstream's own function object" promise on the OFF path would be false while
    `__module__`/`__qualname__` said otherwise; `__code__` is what wraps does not
    copy, so that is what is checked.
    """
    global _upstream_resolve_products

    if _upstream_resolve_products is not None:
        return
    if "saleor.graphql.api" in sys.modules:
        raise RuntimeError(
            "saleor.graphql.api was imported before saleor.wsm.ready(); the "
            "Category.resolve_products swap would be a silent no-op. Move "
            "'saleor.wsm' above whatever imports the schema at import time "
            "(check settings.PLUGINS), or stop importing it there."
        )
    # `Category.resolve_products` is a staticmethod, so this is the plain function.
    upstream = Category.resolve_products
    if upstream.__code__.co_qualname != "Category.resolve_products":
        raise RuntimeError(
            "Category.resolve_products is already wrapped by "
            f"{upstream.__code__.co_qualname!r}; saleor.wsm refuses to stack on "
            "another patch, because the feature-off path must call upstream's own "
            "function object."
        )
    _upstream_resolve_products = upstream
    # The swap is the point of this module; mypy sees only a method assignment.
    Category.resolve_products = staticmethod(resolve_products)  # type: ignore[method-assign]


def resolve_products(root, info, *, channel=None, **kwargs):
    """Run upstream's resolver, or the union of both membership legs.

    The probe result is computed once per resolve, outside the promise closure.
    """
    connection_name = get_database_connection_name(info.context)
    if not secondary_categories_active(connection_name):
        return _upstream_resolve_products(root, info, channel=channel, **kwargs)  # type: ignore[misc]
    return _resolve_products_with_secondary(
        root, info, channel, connection_name, kwargs
    )


def _resolve_products_with_secondary(root, info, channel, connection_name, kwargs):
    """Run a copy of upstream's resolver body, deviating in exactly two places.

    Marked `WSM-FORK` inline: the primary-category filter is skipped, and the
    union is applied after `filter_connection_queryset` so that both membership
    legs inherit the visibility, search, filter and where clauses above it.
    Everything else is upstream's, verbatim, and pinned by the digest above.
    """
    validate_and_apply_search_rank_sorting(
        kwargs, ProductOrderField.RANK, "ProductOrder", info
    )
    search = kwargs.get("search")
    requestor = get_user_or_app_from_context(info.context)
    has_required_permissions = has_one_of_permissions(
        requestor, ALL_PRODUCTS_PERMISSIONS
    )
    tree = root.get_descendants(include_self=True)
    limited_channel_access = False if channel is None else True
    if channel is None and not has_required_permissions:
        channel = get_default_channel_slug_or_graphql_error(
            allow_replica=info.context.allow_replica
        )

    def _resolve_products(channel_obj):
        qs = models.Product.objects.using(connection_name).all()
        if not has_required_permissions:
            qs = (
                qs.visible_to_user(requestor, channel_obj, limited_channel_access)
                .annotate_visible_in_listings(channel_obj)
                .exclude(
                    visible_in_listings=False,
                )
            )
        if channel_obj and has_required_permissions:
            qs = qs.filter(channel_listings__channel_id=channel_obj.id)
        # WSM-FORK: upstream filters `category__in=tree` here. Skipped, because a
        # product also belongs through a PartsLogic secondary assignment; both
        # legs get their own membership predicate below instead.

        if search:
            channel_qs = ChannelQsContext(
                qs=prefix_search(qs, search), channel_slug=channel
            )
        else:
            channel_qs = ChannelQsContext(qs=qs, channel_slug=channel)

        kwargs["channel"] = channel
        channel_qs = filter_connection_queryset(
            channel_qs, kwargs, allow_replica=info.context.allow_replica
        )
        # WSM-FORK: applied after the filtering above, so that both membership
        # legs inherit it.
        channel_qs = with_secondary_categories(channel_qs, tree)
        return create_connection_slice(
            channel_qs, info, kwargs, ProductCountableConnection
        )

    if channel:
        return (
            ChannelBySlugLoader(info.context).load(str(channel)).then(_resolve_products)
        )
    return _resolve_products(None)
