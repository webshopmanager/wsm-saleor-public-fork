# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's queries and mutations, as two mixins.

Reads on a GROUP are the one place this domain's permission is not
MANAGE_DISCOUNTS alone. The Compose option-set screen offers a picker of buyer
group CODES for its tier deltas (`compose/models.py:898` stores the code as a
bare string), and that screen is gated on MANAGE_PRODUCTS: without the second
permission here, a merchant editing an option set would be handed an empty
picker and type a code that prices nothing. Amendment of 2026-09-10. Every
dealer WRITE stays MANAGE_DISCOUNTS.
"""

import graphene
from django.db.models import Count

from ....graphql.core import ResolveInfo
from ....graphql.core.connection import (
    create_connection_slice,
    filter_connection_queryset,
)
from ....graphql.core.context import get_database_connection_name
from ....graphql.core.fields import FilterConnectionField, PermissionsField
from ....graphql.core.utils import from_global_id_or_error
from ....graphql.core.validators import validate_one_of_args_is_in_query
from ....permission.enums import DiscountPermissions, ProductPermissions
from ...dealer import models
from ..types import DOC_CATEGORY_WSM
from .filters import (
    WsmDealerCustomerFilterInput,
    WsmDealerGroupFilterInput,
    WsmTierPriceFilterInput,
)
from .mutations import (
    WsmDealerCustomerAssign,
    WsmDealerCustomerUnassign,
    WsmDealerCustomerUpdate,
    WsmDealerGroupBulkDelete,
    WsmDealerGroupCreate,
    WsmDealerGroupDelete,
    WsmDealerGroupUpdate,
    WsmDealerSettingsUpdate,
    WsmTierPriceBulkCreate,
    WsmTierPriceBulkDelete,
    WsmTierPriceCreate,
    WsmTierPriceDelete,
    WsmTierPriceUpdate,
)
from .types import (
    CUSTOMER_COUNT,
    TIER_PRICE_COUNT,
    WsmDealerCustomer,
    WsmDealerCustomerCountableConnection,
    WsmDealerGroup,
    WsmDealerGroupCountableConnection,
    WsmDealerSettings,
    WsmTierPrice,
    WsmTierPriceCountableConnection,
)

DEALER_PERMISSIONS = [DiscountPermissions.MANAGE_DISCOUNTS]
# Read-only, and only on the group: see the module docstring.
DEALER_GROUP_READ_PERMISSIONS = [
    DiscountPermissions.MANAGE_DISCOUNTS,
    ProductPermissions.MANAGE_PRODUCTS,
]


def _groups(info: ResolveInfo):
    """Every group query, with both of its counts already answered.

    Both counts are on the LIST screen, so a per-row count would be 2N queries
    to render a table of five rows. Annotating them costs the same one query the
    list was already doing.
    """
    # Named connection, not a bare `.objects`: under
    # ENABLE_RESTRICT_WRITER_MIDDLEWARE an unrouted read inside a GraphQL
    # request raises UnsafeWriterAccessError.
    return models.DealerGroup.objects.using(
        get_database_connection_name(info.context)
    ).annotate(
        **{
            TIER_PRICE_COUNT: Count("tier_prices", distinct=True),
            CUSTOMER_COUNT: Count("customers", distinct=True),
        }
    )


def _tier_prices(info: ResolveInfo):
    """Tier prices with everything a 300-row grid renders, in four queries.

    `variant__channel_listings` is prefetched because `currencyCode` on the row
    is what labels the money column, and reading it off the row instead would be
    one query per line of the paste the merchant just made.
    """
    return (
        models.TierPrice.objects.using(get_database_connection_name(info.context))
        .select_related("variant", "variant__product", "group")
        .prefetch_related("variant__channel_listings")
    )


class WsmDealerQueries(graphene.ObjectType):
    wsm_dealer_group = PermissionsField(
        WsmDealerGroup,
        id=graphene.Argument(graphene.ID, description="ID of the group."),
        code=graphene.Argument(graphene.String, description="Code of the group."),
        description="Look up a buyer group by ID, or by the code other tables use.",
        permissions=DEALER_GROUP_READ_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_dealer_groups = FilterConnectionField(
        WsmDealerGroupCountableConnection,
        filter=WsmDealerGroupFilterInput(description="Filtering options for groups."),
        description="List of buyer groups.",
        permissions=DEALER_GROUP_READ_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_dealer_customer = PermissionsField(
        WsmDealerCustomer,
        id=graphene.Argument(graphene.ID, description="ID of the assignment."),
        user=graphene.Argument(graphene.ID, description="ID of the shopper."),
        description="Look up one shopper's group assignment, by row or by user.",
        permissions=DEALER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_dealer_customers = FilterConnectionField(
        WsmDealerCustomerCountableConnection,
        filter=WsmDealerCustomerFilterInput(
            description="Filtering options for dealer customers."
        ),
        description="List of shoppers in a buyer group.",
        permissions=DEALER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_tier_price = PermissionsField(
        WsmTierPrice,
        id=graphene.Argument(
            graphene.ID, required=True, description="ID of the tier price."
        ),
        description="Look up one quantity break.",
        permissions=DEALER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_tier_prices = FilterConnectionField(
        WsmTierPriceCountableConnection,
        filter=WsmTierPriceFilterInput(
            description="Filtering options for tier prices."
        ),
        description="List of quantity breaks.",
        permissions=DEALER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_dealer_settings = PermissionsField(
        WsmDealerSettings,
        required=True,
        description="Store-wide dealer pricing settings.",
        permissions=DEALER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )

    @staticmethod
    def resolve_wsm_dealer_group(_root, info: ResolveInfo, /, *, id=None, code=None):
        validate_one_of_args_is_in_query("id", id, "code", code, use_camel_case=True)
        qs = _groups(info)
        if code:
            return qs.filter(code=code).first()
        _, pk = from_global_id_or_error(id, "WsmDealerGroup", raise_error=True)
        return qs.filter(pk=pk).first()

    @staticmethod
    def resolve_wsm_dealer_groups(_root, info: ResolveInfo, /, **kwargs):
        qs = filter_connection_queryset(
            _groups(info), kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmDealerGroupCountableConnection
        )

    @staticmethod
    def resolve_wsm_dealer_customer(_root, info: ResolveInfo, /, *, id=None, user=None):
        validate_one_of_args_is_in_query("id", id, "user", user, use_camel_case=True)
        qs = models.DealerCustomer.objects.using(
            get_database_connection_name(info.context)
        ).select_related("user", "group")
        if user:
            _, pk = from_global_id_or_error(user, "User", raise_error=True)
            return qs.filter(user_id=pk).first()
        _, pk = from_global_id_or_error(id, "WsmDealerCustomer", raise_error=True)
        return qs.filter(pk=pk).first()

    @staticmethod
    def resolve_wsm_dealer_customers(_root, info: ResolveInfo, /, **kwargs):
        qs = models.DealerCustomer.objects.using(
            get_database_connection_name(info.context)
        ).select_related("user", "group")
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmDealerCustomerCountableConnection
        )

    @staticmethod
    def resolve_wsm_tier_price(_root, info: ResolveInfo, /, *, id):
        _, pk = from_global_id_or_error(id, "WsmTierPrice", raise_error=True)
        return _tier_prices(info).filter(pk=pk).first()

    @staticmethod
    def resolve_wsm_tier_prices(_root, info: ResolveInfo, /, **kwargs):
        qs = filter_connection_queryset(
            _tier_prices(info), kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmTierPriceCountableConnection
        )

    @staticmethod
    def resolve_wsm_dealer_settings(_root, info: ResolveInfo):
        return (
            models.DealerSettings.objects.using(
                get_database_connection_name(info.context)
            ).first()
            or models.DealerSettings()
        )


class WsmDealerMutations(graphene.ObjectType):
    wsm_dealer_group_create = WsmDealerGroupCreate.Field()
    wsm_dealer_group_update = WsmDealerGroupUpdate.Field()
    wsm_dealer_group_delete = WsmDealerGroupDelete.Field()
    wsm_dealer_group_bulk_delete = WsmDealerGroupBulkDelete.Field()
    wsm_dealer_customer_assign = WsmDealerCustomerAssign.Field()
    wsm_dealer_customer_update = WsmDealerCustomerUpdate.Field()
    wsm_dealer_customer_unassign = WsmDealerCustomerUnassign.Field()
    wsm_tier_price_create = WsmTierPriceCreate.Field()
    wsm_tier_price_update = WsmTierPriceUpdate.Field()
    wsm_tier_price_delete = WsmTierPriceDelete.Field()
    wsm_tier_price_bulk_delete = WsmTierPriceBulkDelete.Field()
    wsm_tier_price_bulk_create = WsmTierPriceBulkCreate.Field()
    wsm_dealer_settings_update = WsmDealerSettingsUpdate.Field()
