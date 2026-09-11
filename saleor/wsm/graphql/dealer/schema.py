# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's queries and mutations, as two mixins."""

import graphene

from ....graphql.core import ResolveInfo
from ....graphql.core.context import get_database_connection_name
from ....graphql.core.fields import PermissionsField
from ....permission.enums import DiscountPermissions
from ...dealer import models
from ..types import DOC_CATEGORY_WSM
from .mutations import WsmDealerSettingsUpdate
from .types import WsmDealerSettings


class WsmDealerQueries(graphene.ObjectType):
    wsm_dealer_settings = PermissionsField(
        WsmDealerSettings,
        required=True,
        description="Store-wide dealer pricing settings.",
        permissions=[DiscountPermissions.MANAGE_DISCOUNTS],
        doc_category=DOC_CATEGORY_WSM,
    )

    @staticmethod
    def resolve_wsm_dealer_settings(_root, info: ResolveInfo):
        # Named connection, not a bare `.objects`: under
        # ENABLE_RESTRICT_WRITER_MIDDLEWARE an unrouted read inside a GraphQL
        # request raises UnsafeWriterAccessError, which is how 164 tests went
        # red on the TNO pass.
        return (
            models.DealerSettings.objects.using(
                get_database_connection_name(info.context)
            ).first()
            or models.DealerSettings()
        )


class WsmDealerMutations(graphene.ObjectType):
    wsm_dealer_settings_update = WsmDealerSettingsUpdate.Field()
