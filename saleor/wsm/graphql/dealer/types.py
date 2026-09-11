# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's GraphQL types."""

import graphene

from ....graphql.core.types import ModelObjectType
from ...dealer import models
from ..types import DOC_CATEGORY_WSM


class WsmDealerSettings(ModelObjectType[models.DealerSettings]):
    """The dealer-pricing toggles, as one object.

    No `id` and no `Node` interface on purpose: the table holds one row or none,
    so there is nothing to look up by id and nothing to page through. The
    resolver hands back an unsaved instance when the row does not exist yet, so
    a merchant screen reads the DEFAULTS rather than a null, which is what the
    Django admin did by creating the row on first visit.
    """

    discount_stacking = graphene.Boolean(
        required=True,
        description=(
            "When false (the default), a line already at a dealer price takes "
            "no voucher, promotion or order-level discount on top."
        ),
    )

    class Meta:
        model = models.DealerSettings
        doc_category = DOC_CATEGORY_WSM
        description = "Store-wide dealer pricing settings."
