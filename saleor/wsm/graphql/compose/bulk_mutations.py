# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two bulk deletes the list screens need, on stock's own base.

`ModelBulkDeleteMutation` already resolves the ids, refuses the ones that are
not of this type, and returns a count; there is nothing for us to add, which is
why these are declarations and not code. There is no bulk delete for compliance
rows on purpose: the contract's compliance list deletes one row at a time,
because a fleet-wide audit view that can clear a hundred Prop 65 disclosures in
one click is a lawsuit with a progress bar.
"""

import graphene

from ....graphql.core.mutations import ModelBulkDeleteMutation
from ....graphql.core.types import NonNullList
from ....permission.enums import ProductPermissions
from ...compose import models
from ..errors import WsmError
from ..types import DOC_CATEGORY_WSM
from .types import WsmFee, WsmOptionSet

MANAGE_PRODUCTS = (ProductPermissions.MANAGE_PRODUCTS,)


class WsmOptionSetBulkDelete(ModelBulkDeleteMutation):
    class Meta:
        description = "Delete option sets."
        model = models.OptionSet
        object_type = WsmOptionSet
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="List of option set IDs to delete."
        )


class WsmFeeBulkDelete(ModelBulkDeleteMutation):
    class Meta:
        description = "Delete charges."
        model = models.Fee
        object_type = WsmFee
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="List of charge IDs to delete."
        )
