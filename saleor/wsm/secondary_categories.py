"""Union PartsLogic's secondary category assignments into `Category.products`.

WSM-FORK: fork-owned file. FORK-NOTES.md holds the measurements and the reasoning
behind every shape here; `saleor.wsm.models.ProductSecondaryCategory` documents
the table and who owns it. This module is the read side.

Do not collapse the two legs into one `OR`-ed queryset. That costs the primary
leg its `category_id` index for the whole statement (a leaf page went 0.15 ms to
306 ms, `totalCount` to an unconditional `Seq Scan`), which is the entire reason
`MultiLegQuerySet` exists.
"""

import logging
from typing import cast

from django.db import ProgrammingError
from django.db.models import BooleanField, F, Func, Min, QuerySet, Subquery

from ..core.db.connection import allow_writer_for_default_connection
from ..graphql.core.context import ChannelQsContext
from .models import (
    SECONDARY_CATEGORIES_TABLE,
    ProductSecondaryCategory,
    disable_secondary_categories,
    is_table_unavailable,
    recoverable_failure,
    secondary_categories_available,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EqualsAnyOfArray",
    "MultiLegQuerySet",
    "secondary_categories_active",
    "with_secondary_categories",
]


def secondary_categories_active(database_connection_name: str) -> bool:
    """Whether `Category.products` should union the secondary table on this database.

    The `allow_writer_for_default_connection` wrapper is load-bearing, not
    decoration: the probe issues statements, and this resolver runs on the writer
    alias inside a mutation payload, where `restrict_writer_middleware` refuses
    any statement outside an `allow_writer()` block. See FORK-NOTES.md.
    """
    with allow_writer_for_default_connection(database_connection_name):
        return secondary_categories_available(database_connection_name)


class EqualsAnyOfArray(Func):
    """`<lhs> = ANY (ARRAY(<subquery>))`: id-set membership behind a plan fence.

    Do not rewrite this as `lhs IN (<subquery>)`. Postgres pulls a bare `IN` up
    into a semi-join and then inverts it, walking all of
    `product_product_slug_key` instead of reading the secondary members: 1,421 ms
    against 31 ms on a 152k-product tenant. `ARRAY()` is an optimization fence
    that stops that. Semantically identical to `IN` here because neither side is
    ever NULL. See FORK-NOTES.md hazard 7 for the measurements.
    """

    output_field = BooleanField()
    conditional = True

    def as_sql(  # type: ignore[override]
        self, compiler, connection, **extra_context
    ) -> tuple[str, tuple]:
        lhs, lhs_params = compiler.compile(self.source_expressions[0])
        # `Subquery` compiles with its own surrounding parentheses, which is
        # exactly the `ARRAY(SELECT ...)` form Postgres wants.
        subquery, subquery_params = compiler.compile(self.source_expressions[1])
        return f"{lhs} = ANY (ARRAY{subquery})", (*lhs_params, *subquery_params)


class MultiLegQuerySet:
    """Several querysets over one model, presented to the connection as their union.

    Stands in for the queryset handed to `create_connection_slice`, so
    `graphql/core/connection.py` needs no change.

    **The interface below is CLOSED on purpose, and that is a fork boundary, not a
    convenience. Do not reinstate a `__getattr__` proxy:** the version that had one
    wrapped `qs_with_collection`'s `aggregate()` dict and turned `sortBy:
    COLLECTION` into a request-time `TypeError`. Anything the connection path or a
    sorter calls that is not implemented here raises `AttributeError`, so an
    upstream pull fails the `sortBy` sweep test in CI rather than a category page
    in production.

    Every queryset-returning method applies to `base` as well as to the legs, so
    the legs only ever differ by their membership predicate, which is what cursor
    stability rests on. The legs must be MUTUALLY DISJOINT, a requirement of
    `count()` alone: slicing and the merge are idempotent either way. The rest of
    the argument, including the row-bound and top-k proofs, is in FORK-NOTES.md.
    """

    __slots__ = ("base", "legs")

    def __init__(self, base: QuerySet, legs: list[QuerySet]):
        self.base = base
        self.legs = tuple(legs)

    @property
    def model(self):
        return self.base.model

    def filter(self, *args, **kwargs) -> "MultiLegQuerySet":
        return self._on_every_queryset("filter", *args, **kwargs)

    def exclude(self, *args, **kwargs) -> "MultiLegQuerySet":
        return self._on_every_queryset("exclude", *args, **kwargs)

    def annotate(self, *args, **kwargs) -> "MultiLegQuerySet":
        return self._on_every_queryset("annotate", *args, **kwargs)

    def order_by(self, *args, **kwargs) -> "MultiLegQuerySet":
        return self._on_every_queryset("order_by", *args, **kwargs)

    def sort_by_attribute(self, *args, **kwargs) -> "MultiLegQuerySet":
        return self._on_every_queryset("sort_by_attribute", *args, **kwargs)

    def aggregate(self, **kwargs) -> dict:
        """`Min` over the union, computed as the minimum of the legs' minima.

        Only `Min` combines this way, so anything else raises rather than quietly
        returning a wrong number. Do not compute the sentinel off `base` or off one
        leg: `base` aggregates the whole catalog and makes the `COLLECTION` cursor
        sensitive to changes outside the tree, and one leg strands the sentinel
        mid-page. Both legs stay tree-scoped.

        **Do not unguard the secondary leg here.** Its statement NAMES the psc
        table, where upstream's single aggregate cannot, and an earlier revision
        that left it outside the guard turned a genuine table loss into a hard 500
        on `sortBy: COLLECTION` for the whole TTL. See FORK-NOTES.md.
        """
        if not kwargs or not all(
            isinstance(expression, Min) for expression in kwargs.values()
        ):
            raise NotImplementedError(
                "MultiLegQuerySet.aggregate combines the legs by taking a "
                "minimum, which is only valid for Min(). Got: "
                f"{sorted(kwargs)}"
            )
        per_leg = [self.legs[0].aggregate(**kwargs)]
        if len(self.legs) > 1:
            per_leg.extend(
                self._degrading(
                    lambda: [leg.aggregate(**kwargs) for leg in self.legs[1:]],
                    # Corroborator: the primary leg's aggregate again. Upstream's
                    # own statement, sharing everything but the psc predicate, and
                    # its value folds into the minimum harmlessly because that
                    # result is already the first element above.
                    lambda: [self.legs[0].aggregate(**kwargs)],
                )
            )
        return {
            alias: min(
                (result[alias] for result in per_leg if result[alias] is not None),
                default=None,
            )
            for alias in kwargs
        }

    def count(self) -> int:
        """Sum the disjoint legs, degrading only the secondary ones.

        `totalCount` resolves from a callable AFTER the resolver returns, so a
        `totalCount`-only query never executes the page statement; this is the only
        place its failure can be caught.

        **Do not pull the primary count inside the guard.** It is upstream's own
        statement and must surface as upstream's would; guarding both together
        answered a primary-count failure with a primary-only `totalCount` at HTTP
        200 and disabled the union for a TTL. **And do not replace the corroborator
        with something that executes nothing** (`lambda: 0` was tried, and silently
        deleted layer 3): its value is discarded on purpose, but the statement it
        runs is the layer. See FORK-NOTES.md.
        """
        primary = self.legs[0].count()

        def corroborate_with_the_primary_count() -> int:
            self.legs[0].count()
            return 0

        return primary + self._degrading(
            lambda: sum(leg.count() for leg in self.legs[1:]),
            corroborate_with_the_primary_count,
        )

    def __getitem__(self, key):
        """Merge the per-leg top-k id lists, executing eagerly when it will be read.

        **Do not make this lazy.** Executing here is what keeps the degradation
        guard around exactly one statement, the merged page query; the channel
        lookup and the sorter builds stay outside it and propagate as upstream's
        would. When `end_margin` is None there is no page to fetch (a
        `totalCount`-only query) and returning the unexecuted queryset keeps that
        statement from ever running.

        Row-bound caveat: `sortBy: COLLECTION` adds no `GROUP BY`, so its per-leg
        `LIMIT` counts JOIN rows rather than distinct products. That is upstream's
        behaviour too and a test pins it. **Do not add grouping to "fix" it**: it
        would break the `ORDER BY` and diverge from upstream's row semantics. See
        FORK-NOTES.md hazards 4 and 5.
        """
        if not isinstance(key, slice):
            raise NotImplementedError(
                "MultiLegQuerySet supports slicing, not indexing: an index would "
                f"have to be resolved against the merged order. Got: {key!r}"
            )
        if key.step is not None:
            # Django returns a list for a strided slice, so the union below would
            # die as `AttributeError: 'list' object has no attribute 'union'`
            # three frames down. Same contract as the offset case: reject what
            # cannot be honoured, loudly.
            raise NotImplementedError(
                f"MultiLegQuerySet cannot apply a step to per-leg slices. Got: {key!r}"
            )
        if key.start:
            # A per-leg OFFSET is not the global OFFSET, so pushing one down would
            # return the union of each leg's own rows N..M and look plausible.
            # `connection_from_queryset_slice` only ever slices `[:end_margin]`;
            # the test suite pins that, so this raises rather than guessing.
            raise NotImplementedError(
                "MultiLegQuerySet cannot push a global OFFSET into per-leg "
                f"slices. Got: {key!r}"
            )
        limited = [leg.values("pk")[key] for leg in self.legs]
        merged = self.base.filter(pk__in=limited[0].union(*limited[1:], all=True))
        if key.stop is None:
            return merged[key]
        return self._degrading(
            lambda: list(merged[key]),
            lambda: list(self.legs[0][key]),
        )

    def _degrading(self, union, corroborating_fallback):
        """Run `union`, falling back if the secondary table is unreadable.

        **`corroborating_fallback` MUST execute at least one statement** sharing
        everything with `union` except the secondary predicate. A fallback that
        executes nothing silently deletes layer 3, so where its value is unusable
        (the count path) it still runs the statement and throws the value away.

        Three layers, all required, each of which fails a test when reverted alone:
        (1) scope, the callable runs only statements that NAME the psc table, and a
        test asserts both directions of that; (2) evidence, the SQLSTATE has to say
        the table itself is the problem; (3) corroboration, the fallback runs
        BEFORE the feature is marked unavailable, so a failure rooted elsewhere
        reproduces in it and surfaces. Degradations are logged because nothing else
        in the stack would ever report one. See FORK-NOTES.md.
        """
        try:
            with recoverable_failure(self.base.db):
                return union()
        except ProgrammingError as error:
            if not is_table_unavailable(error):
                raise
            answer = corroborating_fallback()
            disable_secondary_categories(self.base.db)
            logger.warning(
                "%s is unreadable (%s); answering %s from the primary category "
                "alone and disabling the union until the probe is retried",
                SECONDARY_CATEGORIES_TABLE,
                error,
                self.base.model.__name__,
                exc_info=True,
            )
            return answer

    def _on_every_queryset(self, name: str, *args, **kwargs) -> "MultiLegQuerySet":
        return MultiLegQuerySet(
            getattr(self.base, name)(*args, **kwargs),
            [getattr(leg, name)(*args, **kwargs) for leg in self.legs],
        )


def with_secondary_categories(
    channel_qs: ChannelQsContext, category_tree: QuerySet
) -> ChannelQsContext:
    """Split a `Category.products` queryset into its membership legs.

    `channel_qs.qs` must be the product queryset with visibility, `search`,
    `filter` and `where` already applied and **no category restriction yet**: every
    leg derives from it, so every leg inherits all of that by construction rather
    than by remembering to repeat it.

    **Do not "optimize" away the `exclude()` on the secondary leg.** It is REQUIRED
    by `count()`, which sums the legs and so needs them disjoint, and on the page
    path (which does not need it) removing it measures SLOWER, not faster: it is
    what makes the secondary leg selective. 151 ms against 351 ms on a
    1,535-category root. Measure first; the full table is in FORK-NOTES.md.
    """
    base = channel_qs.qs
    secondary_product_ids = Subquery(
        ProductSecondaryCategory.objects.using(base.db)
        .filter(category_id__in=category_tree.values("pk"))
        .values("product_id")
    )
    legs = MultiLegQuerySet(
        base,
        [
            base.filter(category__in=category_tree),
            base.filter(EqualsAnyOfArray(F("pk"), secondary_product_ids)).exclude(
                category__in=category_tree
            ),
        ],
    )
    # A stand-in for a QuerySet rather than one: see the class docstring.
    return ChannelQsContext(
        qs=cast(QuerySet, legs), channel_slug=channel_qs.channel_slug
    )
