# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's GraphQL mutations."""

import graphene

from ....graphql.core.mutations import DeprecatedModelMutation
from ....graphql.core.types import BaseInputObjectType
from ....permission.enums import DiscountPermissions
from ...dealer import models
from ..errors import WsmError
from ..types import DOC_CATEGORY_WSM
from .types import WsmDealerSettings


class WsmDealerSettingsInput(BaseInputObjectType):
    discount_stacking = graphene.Boolean(
        required=True,
        description=(
            "False: a dealer-priced line takes no further discount. True: "
            "discounts combine with dealer prices."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmDealerSettingsUpdate(DeprecatedModelMutation):
    """The singleton update.

    `DeprecatedModelMutation` is what stock's own model mutations still use
    (`saleor/graphql/giftcard/mutations/gift_card_create.py`); there is no
    non-deprecated `ModelMutation` in 3.23 to match instead.

    ponytail: `MANAGE_DISCOUNTS` is reused rather than a `WsmPermissions` enum
    added, because the enum lives in a core file and the codenames would have to
    reach graphene's `PermissionEnum` to be assignable in the Dashboard's own
    permission-group screens. Ceiling: anyone who can edit vouchers can edit
    this. Upgrade path: append a `WsmPermissions` class to `PERMISSIONS_ENUMS`
    from `ready()` before the schema is built, at which point the existing
    `create_wsm_permissions` receiver and the `wsm_merchant_role` command
    already carry the rows and the group.
    """

    class Arguments:
        input = WsmDealerSettingsInput(
            required=True, description="Fields required to update dealer settings."
        )

    class Meta:
        description = "Update the store-wide dealer pricing settings."
        model = models.DealerSettings
        object_type = WsmDealerSettings
        permissions = (DiscountPermissions.MANAGE_DISCOUNTS,)
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def get_instance(cls, info, **data):
        """One row or none, never a second one.

        The inherited implementation reads `data["id"]` and, finding none,
        returns `model()`: a FRESH instance, which on save would give this
        singleton table a second row on every call. There is no id to take,
        because the row is the store, so the existing row is the instance.
        """
        return models.DealerSettings.objects.first() or models.DealerSettings()
