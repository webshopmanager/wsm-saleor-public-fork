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


class WsmDealerGroup(ModelObjectType[models.DealerGroup]):
    """A buyer group: dealer-1, warehouse, installer.

    OWNERSHIP NOTE: this type belongs to the dealer domain (unit U4). It is
    declared here by the compose unit because the contract puts
    `WsmDealerTierOptionPrice.dealerGroup` on a compose row: a tier delta names
    a group by CODE (a CharField, deliberately, so compose can price without
    joining wsm_dealer) and the option-set screen wants the group's NAME beside
    the code. The shape is the contract's, verbatim; when U4 lands its own
    version of this file, keep U4's and delete this block.
    """

    id = graphene.GlobalID(required=True, description="ID of the dealer group.")
    code = graphene.String(
        required=True,
        description=(
            "The exact code compose tier rows and the storefront name this "
            "group by. Unique, and never changed once prices point at it."
        ),
    )
    name = graphene.String(
        required=True, description="Empty string means staff see the code."
    )
    tier_price_count = graphene.Int(
        required=True,
        description=(
            "A count, not a list: live data is 304+ tier rows per group, which "
            "is why the admin showed an aggregate instead of an inline."
        ),
    )
    customer_count = graphene.Int(
        required=True, description="How many shoppers are in this group."
    )

    class Meta:
        description = "A buyer group: dealer-1, warehouse, installer."
        model = models.DealerGroup
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_tier_price_count(root, info):
        from .dataloaders import TierPriceCountByDealerGroupIdLoader

        return TierPriceCountByDealerGroupIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_customer_count(root, info):
        from .dataloaders import CustomerCountByDealerGroupIdLoader

        return CustomerCountByDealerGroupIdLoader(info.context).load(root.id)
