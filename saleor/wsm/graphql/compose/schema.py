# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's queries and mutations, as two mixins.

Six query fields and ten mutations, every one gated on `MANAGE_PRODUCTS`
(decision D1: stock enum members only, zero new Permission rows per tenant).

ponytail: `MANAGE_PRODUCTS` also buys the dealer deltas that hang off an option
value, because `WsmDealerTierOptionPrice` is a compose row. Ceiling: a staffer
who can edit products can see one buyer group's option pricing. Upgrade path is
a rung-5 append of a `WsmPermissions` class onto `PERMISSIONS_ENUMS` from
`ready()`, a follow-up unit, not this one.
"""

import graphene
from django.core.exceptions import ValidationError

from ....graphql.core import ResolveInfo
from ....graphql.core.connection import (
    create_connection_slice,
    filter_connection_queryset,
)
from ....graphql.core.context import get_database_connection_name
from ....graphql.core.fields import FilterConnectionField, PermissionsField
from ....graphql.core.utils import from_global_id_or_error
from ....permission.enums import ProductPermissions
from ...compose import models
from ..types import DOC_CATEGORY_WSM

# Importing the module is what appends the three fields to stock's `Product`.
# Its position in this block does not matter: the ordering that does is inside
# that module, which imports saleor.graphql.api itself, so stock builds its own
# schema before anything is appended to its Product type.
from . import product_extension  # noqa: F401  (imported for effect)
from .bulk_mutations import WsmFeeBulkDelete, WsmOptionSetBulkDelete
from .filters import (
    WsmFeeFilterInput,
    WsmOptionSetFilterInput,
    WsmProductComplianceFilterInput,
)
from .mutations import (
    WsmFeeCreate,
    WsmFeeDelete,
    WsmFeeUpdate,
    WsmOptionSetCreate,
    WsmOptionSetDelete,
    WsmOptionSetUpdate,
    WsmProductComplianceDelete,
    WsmProductComplianceUpdate,
)
from .types import (
    WsmFee,
    WsmFeeCountableConnection,
    WsmOptionSet,
    WsmOptionSetCountableConnection,
    WsmProductCompliance,
    WsmProductComplianceCountableConnection,
)

MANAGE_PRODUCTS = [ProductPermissions.MANAGE_PRODUCTS]


def _reader(model, info):
    """Every list and detail read on the request's own connection.

    A bare `.objects` here raises UnsafeWriterAccessError under
    ENABLE_RESTRICT_WRITER_MIDDLEWARE, which is how 164 tests went red on the
    TNO pass. One helper so no resolver below can forget.
    """
    return model.objects.using(get_database_connection_name(info.context))


def _by_id(model, graphql_type, info, object_id):
    _type, pk = from_global_id_or_error(object_id, graphql_type, raise_error=True)
    return _reader(model, info).filter(pk=pk).first()


class ComposeQueries(graphene.ObjectType):
    wsm_option_set = PermissionsField(
        WsmOptionSet,
        id=graphene.Argument(
            graphene.ID, description="ID of the option set.", required=True
        ),
        description="Look up one option set by ID.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_option_sets = FilterConnectionField(
        WsmOptionSetCountableConnection,
        filter=WsmOptionSetFilterInput(
            description="Filtering options for option sets."
        ),
        description="List of option sets.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_fee = PermissionsField(
        WsmFee,
        id=graphene.Argument(
            graphene.ID, description="ID of the charge.", required=True
        ),
        description="Look up one charge by ID.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_fees = FilterConnectionField(
        WsmFeeCountableConnection,
        filter=WsmFeeFilterInput(description="Filtering options for charges."),
        description="List of charges.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_product_compliance = PermissionsField(
        WsmProductCompliance,
        id=graphene.Argument(graphene.ID, description="ID of the compliance row."),
        product=graphene.Argument(graphene.ID, description="ID of the product."),
        description="Look up a compliance row by row ID, or by product.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_product_compliances = FilterConnectionField(
        WsmProductComplianceCountableConnection,
        filter=WsmProductComplianceFilterInput(
            description="Filtering options for compliance rows."
        ),
        description="List of product compliance rows.",
        permissions=MANAGE_PRODUCTS,
        doc_category=DOC_CATEGORY_WSM,
    )

    @staticmethod
    def resolve_wsm_option_set(_root, info: ResolveInfo, /, *, id: str):
        return _by_id(models.OptionSet, WsmOptionSet, info, id)

    @staticmethod
    def resolve_wsm_option_sets(_root, info: ResolveInfo, /, **kwargs):
        qs = _reader(models.OptionSet, info)
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmOptionSetCountableConnection
        )

    @staticmethod
    def resolve_wsm_fee(_root, info: ResolveInfo, /, *, id: str):
        return _by_id(models.Fee, WsmFee, info, id)

    @staticmethod
    def resolve_wsm_fees(_root, info: ResolveInfo, /, **kwargs):
        qs = _reader(models.Fee, info)
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(qs, info, kwargs, WsmFeeCountableConnection)

    @staticmethod
    def resolve_wsm_product_compliance(
        _root, info: ResolveInfo, /, *, id=None, product=None
    ):
        """By row ID, or by product: exactly one of the two.

        Both, or neither, is a caller bug and gets said so rather than silently
        preferring one: a screen that sends both is asking two questions and
        will render the answer to whichever this code happened to pick.
        """
        if bool(id) == bool(product):
            raise ValidationError(
                "Give exactly one of `id` or `product`.", code="graphql_error"
            )
        if id:
            return _by_id(models.ProductCompliance, WsmProductCompliance, info, id)
        from ....graphql.product.types import Product

        _type, pk = from_global_id_or_error(product, Product, raise_error=True)
        return _reader(models.ProductCompliance, info).filter(product_id=pk).first()

    @staticmethod
    def resolve_wsm_product_compliances(_root, info: ResolveInfo, /, **kwargs):
        # No `Meta.ordering` on this model, and a connection slice with no
        # deterministic order pages rows twice and skips others.
        qs = _reader(models.ProductCompliance, info).order_by("pk")
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmProductComplianceCountableConnection
        )


class ComposeMutations(graphene.ObjectType):
    wsm_option_set_create = WsmOptionSetCreate.Field()
    wsm_option_set_update = WsmOptionSetUpdate.Field()
    wsm_option_set_delete = WsmOptionSetDelete.Field()
    wsm_option_set_bulk_delete = WsmOptionSetBulkDelete.Field()
    wsm_fee_create = WsmFeeCreate.Field()
    wsm_fee_update = WsmFeeUpdate.Field()
    wsm_fee_delete = WsmFeeDelete.Field()
    wsm_fee_bulk_delete = WsmFeeBulkDelete.Field()
    wsm_product_compliance_update = WsmProductComplianceUpdate.Field()
    wsm_product_compliance_delete = WsmProductComplianceDelete.Field()
