# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Containers' GraphQL types: the series, the kit, its parts and its rules.

A container IS a Saleor Collection (ruling of 2026-09-08), so every type here
hangs off one: `collection` is the stock `Collection` type and the Dashboard
reaches the products through it, never through us. What these types add is the
only thing Saleor has no column for, which is the same thing the tables add.

The three nested types (member, rule, and the rule's targets) are fields on
their parent rather than top-level surface, for the reason the admin had no
standalone screens for them: a rule is about the parts beside it, so the set is
what a merchant reads and what a mutation has to check at once.
"""

import graphene

from ....graphql.core.connection import CountableConnection
from ....graphql.core.context import ChannelContext, get_database_connection_name
from ....graphql.core.scalars import PositiveDecimal
from ....graphql.core.types import ModelObjectType, NonNullList
from ....graphql.product.types.collections import Collection
from ....graphql.product.types.products import ProductVariant
from ...containers import models, pricing
from ..types import DOC_CATEGORY_WSM

# Built from the models' own vocabulary rather than re-spelled: the wire name is
# the merchant-facing one, the value is what the pricing module branches on, and
# a third spelling of "percent" is a defect waiting for a release.
WsmKitDiscountKind = graphene.Enum(
    "WsmKitDiscountKind",
    [("FIXED", pricing.FIXED), ("PERCENT", pricing.PERCENT)],
)
WsmKitDiscountKind.doc_category = DOC_CATEGORY_WSM

WsmKitRuleKind = graphene.Enum(
    "WsmKitRuleKind",
    [("REQUIRES_ONE_OF", models.REQUIRES_ONE_OF), ("EXCLUDES", models.EXCLUDES)],
)
WsmKitRuleKind.doc_category = DOC_CATEGORY_WSM


def _collection(collection):
    """The stock Collection type, as the stock type expects to be handed one.

    `Collection` is a `ChannelContextType`, so its default resolver reads
    `root.node`: a bare model instance renders every field as null. No channel,
    because a merchant screen is not shopping in one.
    """
    return ChannelContext(node=collection, channel_slug=None)


class WsmSeriesConfig(ModelObjectType[models.SeriesConfig]):
    """The ONE series editor. The Collection's `wsm.series` blob is its output."""

    id = graphene.GlobalID(required=True, description="ID of the series.")
    collection = graphene.Field(
        Collection,
        required=True,
        description="The collection whose products this series configures.",
    )
    brand = graphene.String(
        required=True, description="The one brand this series covers."
    )
    axes = NonNullList(
        graphene.String,
        required=True,
        description=(
            "The questions the configurator asks, in order, as product attribute slugs."
        ),
    )
    partitioning_axis = graphene.String(
        required=True,
        description="The one axis that decides which product the shopper lands on.",
    )
    miss_message = graphene.String(
        required=True,
        description="What a shopper is told when their answers match nothing.",
    )
    published = graphene.Boolean(
        required=True, description="Whether the storefront shows this series."
    )
    member_count = graphene.Int(
        required=True,
        description=(
            "Published products in the collection. The number the publish rule "
            "is about, so a merchant reads it beside the refusal."
        ),
    )

    class Meta:
        model = models.SeriesConfig
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM
        description = "A series: the questions a configurator asks about a collection."

    @staticmethod
    def resolve_collection(root: models.SeriesConfig, _info):
        return _collection(root.collection)

    @staticmethod
    def resolve_axes(root: models.SeriesConfig, _info):
        return list(root.axes or [])

    @staticmethod
    def resolve_member_count(root: models.SeriesConfig, info):
        # One aggregate, and the same one the admin's readonly row showed
        # (`containers/admin.py:168`): published members, because that is what
        # the publish rule counts.
        from ....product.models import Product

        return (
            Product.objects.using(get_database_connection_name(info.context))
            .filter(
                collections__id=root.collection_id, channel_listings__is_published=True
            )
            .distinct()
            .count()
        )


class WsmKitMember(ModelObjectType[models.KitMember]):
    id = graphene.GlobalID(required=True, description="ID of the kit member.")
    kit = graphene.Field(
        lambda: WsmKitConfig, required=True, description="The kit this part is in."
    )
    variant = graphene.Field(
        ProductVariant, required=True, description="The SKU this kit contains."
    )
    quantity = graphene.Int(
        required=True, description="How many of this SKU one kit contains."
    )
    sort_order = graphene.Int(required=True, description="Lowest first.")

    class Meta:
        model = models.KitMember
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM
        description = "One variant in a kit, and how many of it the kit contains."

    @staticmethod
    def resolve_variant(root: models.KitMember, _info):
        return ChannelContext(node=root.variant, channel_slug=None)


class WsmKitMemberRule(ModelObjectType[models.KitMemberRule]):
    id = graphene.GlobalID(required=True, description="ID of the rule.")
    kit = graphene.Field(
        lambda: WsmKitConfig, required=True, description="The kit this rule is in."
    )
    subject = graphene.Field(
        WsmKitMember, required=True, description="The part this rule is about."
    )
    kind = graphene.Field(
        WsmKitRuleKind,
        required=True,
        description="Needs one of, or cannot be sold with.",
    )
    targets = NonNullList(
        WsmKitMember,
        required=True,
        description="The other parts of this same kit the rule is about.",
    )
    message = graphene.String(
        required=True,
        description="What the shopper is told when this rule stops the kit.",
    )

    class Meta:
        model = models.KitMemberRule
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM
        description = "One member needs, or refuses, another."

    @staticmethod
    def resolve_targets(root: models.KitMemberRule, _info):
        return root.targets.all()


class WsmKitConfig(ModelObjectType[models.KitConfig]):
    id = graphene.GlobalID(required=True, description="ID of the kit.")
    collection = graphene.Field(
        Collection, required=True, description="The collection this kit is sold as."
    )
    discount_kind = graphene.Field(
        WsmKitDiscountKind,
        required=True,
        description="Whether the saving is money off the kit or a share of it.",
    )
    discount_amount = PositiveDecimal(
        required=True,
        description="The saving off the members' own prices added up.",
    )
    freight_class = graphene.String(
        required=True, description="Freight class for the kit as one shipment."
    )
    active = graphene.Boolean(
        required=True, description="Off takes the kit price away."
    )
    members = NonNullList(
        WsmKitMember, required=True, description="Ordered by sort order, then id."
    )
    rules = NonNullList(
        WsmKitMemberRule, required=True, description="How this kit goes together."
    )
    currency_code = graphene.String(
        description=(
            "The currency the members are priced in, so a merchant screen can "
            "label the discount box without a second round trip."
        )
    )

    class Meta:
        model = models.KitConfig
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM
        description = (
            "What a Collection needs to behave as a kit: a discount, and members."
        )

    @staticmethod
    def resolve_collection(root: models.KitConfig, _info):
        return _collection(root.collection)

    @staticmethod
    def resolve_members(root: models.KitConfig, _info):
        return root.members.all()

    @staticmethod
    def resolve_rules(root: models.KitConfig, _info):
        return root.rules.all()

    @staticmethod
    def resolve_currency_code(root: models.KitConfig, info):
        """What the members are priced in, in one query, from the members.

        A kit carries no currency column because no WSM column does: the honest
        answer is the one the merchant's own listing gives, and the shop's
        channel is the fallback while a kit is still empty.
        ponytail: one query per kit, which is a detail screen, not a list; a
        list that ever selects this field wants a dataloader instead.
        """
        from ....channel.models import Channel
        from ....product.models import ProductVariantChannelListing

        database = get_database_connection_name(info.context)
        currency = (
            ProductVariantChannelListing.objects.using(database)
            .filter(variant__wsm_kit_memberships__kit_id=root.pk)
            .values_list("currency", flat=True)
            .first()
        )
        if currency:
            return currency
        return (
            Channel.objects.using(database)
            .values_list("currency_code", flat=True)
            .first()
        )


class WsmSeriesConfigCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmSeriesConfig


class WsmKitConfigCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmKitConfig
