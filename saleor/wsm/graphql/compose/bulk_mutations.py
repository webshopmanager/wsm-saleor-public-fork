# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two bulk deletes the list screens need, on stock's own base.

`ModelBulkDeleteMutation` already resolves the ids, refuses the ones that are
not of this type, and returns a count, so the only thing added here is the
layer's own bulk cap: an uncapped option-set delete cascades into option values
and their dealer deltas, and stock leaves its own deletes unbounded. There is no bulk delete for compliance
rows on purpose: the contract's compliance list deletes one row at a time,
because a fleet-wide audit view that can clear a hundred Prop 65 disclosures in
one click is a lawsuit with a progress bar.
"""

import graphene

from ....graphql.core.mutations import ModelBulkDeleteMutation
from ....graphql.core.types import NonNullList
from ....permission.enums import ProductPermissions
from ...compose import models
from ..errors import WsmMutationMeta
from ..utils import BulkLimitMixin
from .types import WsmFee, WsmOptionSet

MANAGE_PRODUCTS = (ProductPermissions.MANAGE_PRODUCTS,)


class WsmOptionSetBulkDelete(WsmMutationMeta, BulkLimitMixin, ModelBulkDeleteMutation):
    class Meta:
        description = "Delete option sets."
        model = models.OptionSet
        object_type = WsmOptionSet
        permissions = MANAGE_PRODUCTS

    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="List of option set IDs to delete."
        )


class WsmFeeBulkDelete(WsmMutationMeta, BulkLimitMixin, ModelBulkDeleteMutation):
    class Meta:
        description = "Delete charges."
        model = models.Fee
        object_type = WsmFee
        permissions = MANAGE_PRODUCTS

    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="List of charge IDs to delete."
        )
