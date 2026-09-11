# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The id helpers and the bulk cap every WSM domain needs, in one layer.

Three branches wrote `_error` and `_pk_or_none` character for character, and a
fourth would have. They live here now, above the domain packages, because a
global id is a layer concern and not a compose, containers or dealer one.

`TypedIdMixin` is the same argument about the same value: the inherited
`clean_input` resolves a bare `ID` input field with no `only_type`, so ANY
global id resolves and a Collection id posted as `product` reaches a foreign-key
assignment and 500s. Saying which type an input names turns that into the field
error the Dashboard already knows how to render.
"""

from django.core.exceptions import ValidationError
from graphql.error import GraphQLError

from ...graphql.core.utils import from_global_id_or_error

# One call is one paste, on every bulk path in the layer. Stock caps its own
# bulk create the same way (`MAX_ORDERS = 50`,
# `saleor/graphql/order/bulk_mutations/order_bulk_create.py:86`) and 50 is the
# shape of an order import, not of a price list: live data is 304 tier rows on
# one group (`dealer/admin.py:32`), so the cap is set above the paste these
# mutations exist for and below the payload that would hold the request open
# building instances nobody can read back. The DELETES take it too: stock
# leaves its own uncapped, but an unbounded option-set bulk delete cascades
# into option values and their dealer deltas, and a mounted route with no
# bound is a blast radius nobody chose.
BULK_LIMIT = 500


def error(message: str, code: str) -> ValidationError:
    return ValidationError(message, code=code)


def bulk_limit_error(count: int, field: str) -> ValidationError:
    return ValidationError(
        {
            field: error(
                f"{count} rows in one call, and the limit is "
                f"{BULK_LIMIT}. Split the paste.",
                "bulk_limit",
            )
        }
    )


def check_bulk_limit(rows, field: str) -> None:
    """The cap on a path whose `mutate` catches ValidationError."""
    if len(rows) > BULK_LIMIT:
        raise bulk_limit_error(len(rows), field)


class BulkLimitMixin:
    """The same cap on a bulk DELETE, RETURNED rather than raised.

    `BaseBulkMutation.mutate` does not catch ValidationError the way
    `BaseMutation.mutate` does (`saleor/graphql/core/mutations.py:1109-1121`):
    it reads a `(count, errors)` pair off `perform_mutation`, so a raise here
    would reach the Dashboard as a top-level GraphQL error instead of the
    field error every other refusal in this layer is.

    Checked before `get_nodes_or_error`, so 5,000 ids cost one `len()` and not
    5,000 decodes and a query.
    """

    @classmethod
    def perform_mutation(cls, root, info, /, *, ids, **data):
        if len(ids) > BULK_LIMIT:
            return 0, bulk_limit_error(len(ids), "ids")
        return super().perform_mutation(root, info, ids=ids, **data)


def pk_or_none(global_id, type_name: str):
    """The database id behind a global id, or None if it is not one of those."""
    try:
        _type, pk = from_global_id_or_error(global_id, type_name, raise_error=True)
        return int(pk)
    except (ValidationError, GraphQLError, ValueError, TypeError, UnicodeDecodeError):
        return None


class TypedIdMixin:
    """Resolve an input FK id to the type that input NAMES, not to any node.

    Every id in a WSM input names exactly one type. `typed_ids` says which, the
    value is pulled out before the inherited `clean_input` can resolve it
    untyped, and `get_node_or_error` puts it back as the instance or raises a
    NOT_FOUND field error.
    """

    # field name in the input -> the graphene type its global id must carry
    typed_ids: dict = {}

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        typed = {name: data.pop(name) for name in cls.typed_ids if name in data}
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        for name, value in typed.items():
            if value is None:
                continue
            cleaned_input[name] = cls.get_node_or_error(
                info, value, field=name, only_type=cls.typed_ids[name]
            )
        return cleaned_input
