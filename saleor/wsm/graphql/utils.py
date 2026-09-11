# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The id helpers every WSM domain needs, in the layer that owns them.

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


def error(message: str, code: str) -> ValidationError:
    return ValidationError(message, code=code)


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
