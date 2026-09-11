# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Ranked SKU search, owned once because two screens now ask the same question.

The Django admin picker wrote this rule first: typing `71801`, a manufacturer
part number, put the product that carries it FIFTH, behind four covers whose
SKUs (`trp:1471801`) merely CONTAIN those digits, because product name was the
only order the picker had. The Dashboard's `wsmOptionSets(filter: {search})`
searches the same rows for the same reason, so the rule lives here and both
callers import it. A copy in each would be two rules a month from now.

Three tiers, one CASE, no extra round trip: the whole term as a SKU, the whole
term as a word inside a SKU or a name, then everything else in the order it
already had. The subqueries are correlated columns on the query the search
already runs, and not joins, because a join on a reverse relation returns the
same row once per variant.
"""

import re

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When

WHOLE_TOKEN_RANK = 1
EXACT_SKU_RANK = 0
EVERYTHING_ELSE_RANK = 2


def whole_token(term: str) -> str:
    """A pattern that matches `term` only where it is not part of a longer run.

    Postgres flavour, for `iregex`. The catalog is full of SKUs that CONTAIN a
    part number without being it: `trp:1471801` contains `71801`.
    """
    return r"(^|[^0-9A-Za-z])" + re.escape(term) + r"([^0-9A-Za-z]|$)"


def _variant_subqueries(queryset, term, variant_field, outer_ref):
    """Exact-SKU and whole-token-SKU existence subqueries, on the reader's db.

    Two halves of one correlation, and both callers need to say both. The
    variant reaches the outer row through `variant_field` (`product` when the
    rows own products, `pk` when the rows ARE variants), and the outer row
    offers `outer_ref` as the thing to match (`pk` for a product or variant
    picker, `product_id` for a compose row hanging off one). Getting the second
    one wrong is silent: the subquery compares a variant's product against an
    option-set id, matches nothing, and the search answers an empty list.

    `.using(queryset.db)` rather than a bare `.objects`: under
    ENABLE_RESTRICT_WRITER_MIDDLEWARE an unrouted read inside a GraphQL request
    raises UnsafeWriterAccessError, which is how 164 tests went red on the TNO
    pass. The admin's connection is the default one, so the same call is correct
    from both callers.
    """
    from ...product.models import ProductVariant

    owner = {variant_field: OuterRef(outer_ref)}
    variants = ProductVariant.objects.using(queryset.db)
    return (
        variants.filter(**owner, sku__iexact=term),
        variants.filter(**owner, sku__iregex=whole_token(term)),
        variants.filter(**owner, sku__icontains=term),
    )


def rank_by_sku(
    queryset, term, *, variant_field="product", outer_ref="pk", name_field="name"
):
    """Order `queryset` so the row the merchant typed comes first.

    Ranking only. The caller has already narrowed the rows; this decides which
    of them a merchant reads first.
    """
    term = (term or "").strip()
    if not term:
        return queryset
    exact, token, _contains = _variant_subqueries(
        queryset, term, variant_field, outer_ref
    )
    name_matches = (
        Q(**{f"{name_field}__iregex": whole_token(term)})
        if name_field
        else Q(pk__in=[])
    )
    return queryset.annotate(
        wsm_match_rank=Case(
            When(Exists(exact), then=Value(EXACT_SKU_RANK)),
            When(Q(Exists(token)) | name_matches, then=Value(WHOLE_TOKEN_RANK)),
            default=Value(EVERYTHING_ELSE_RANK),
            output_field=IntegerField(),
        )
    )


def search_and_rank(queryset, term, *, product_path="product", name_fields=("name",)):
    """Narrow to rows that match `term`, then rank them, in one query.

    The admin gets its narrowing from `search_fields`; a GraphQL filter has no
    such thing, so the two halves are said here together. `name_fields` are this
    model's own merchant-readable columns, and the product's own name is always
    searched too, because a merchant looking for "the crating charge on the
    QSST" types the product, not the charge.
    """
    term = (term or "").strip()
    if not term:
        return queryset
    outer_ref = f"{product_path}_id"
    _exact, _token, contains = _variant_subqueries(queryset, term, "product", outer_ref)
    matches = Q(Exists(contains)) | Q(**{f"{product_path}__name__icontains": term})
    for field in name_fields:
        matches |= Q(**{f"{field}__icontains": term})
    ranked = rank_by_sku(
        queryset.filter(matches),
        term,
        outer_ref=outer_ref,
        name_field=name_fields[0] if name_fields else "",
    )
    return ranked.order_by("wsm_match_rank", *(queryset.query.order_by or ("pk",)))
