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
from ....graphql.core.context import ChannelContext
from ....graphql.core.types import BaseObjectType, ModelObjectType, NonNullList
from ....graphql.product.types.collections import Collection
from ....graphql.product.types.products import ProductVariant
from ...containers import models, pricing, resolve
from ..scalars import WsmDecimal
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

# What the search engine says about one candidate, for one vehicle. Built from
# the resolver's own constants for the same reason the two above are built from
# the models': a second spelling of "universal" is a release waiting to break.
WsmContainerFitment = graphene.Enum(
    "WsmContainerFitment",
    [
        ("FITS", resolve.FITS),
        ("UNIVERSAL", resolve.UNIVERSAL),
        ("UNKNOWN", resolve.UNKNOWN),
        ("UNFILTERED", resolve.UNFILTERED),
    ],
)
WsmContainerFitment.doc_category = DOC_CATEGORY_WSM

# Why a container could not be resolved. Its own enum and NOT `WsmErrorCode`,
# because a refusal is an ANSWER a storefront draws, not a mutation error: the
# merchant's sentence rides beside it and the query still returns 200.
WsmContainerRefusalCode = graphene.Enum(
    "WsmContainerRefusalCode",
    [
        ("NOT_CONFIGURED", resolve.REFUSAL_NOT_A_CONTAINER),
        ("SLOT_HAS_NO_FIT", resolve.REFUSAL_SLOT_HAS_NO_FIT),
        ("ENGINE_UNAVAILABLE", resolve.REFUSAL_ENGINE_UNAVAILABLE),
        ("BAD_COLLECTION_ID", resolve.REFUSAL_BAD_COLLECTION_ID),
        ("TOO_LARGE_TO_RESOLVE", resolve.REFUSAL_TRUNCATED),
    ],
)
WsmContainerRefusalCode.doc_category = DOC_CATEGORY_WSM


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
        # the publish rule counts. Batched, because the series LIST selects it
        # once per row and an inline query there is 1+N.
        from .dataloaders import MemberCountByCollectionIdLoader

        return MemberCountByCollectionIdLoader(info.context).load(root.collection_id)


class WsmContainerSlot(ModelObjectType[models.ContainerSlot]):
    """One named role inside a container: what can go here, and how it is chosen."""

    id = graphene.GlobalID(required=True, description="ID of the slot.")
    kit = graphene.Field(
        lambda: WsmKitConfig,
        required=True,
        description="The container this role is in.",
    )
    label = graphene.String(
        required=True, description="What this part of the container is called."
    )
    quantity = graphene.Int(
        required=True, description="How many of whatever fills this slot."
    )
    required = graphene.Boolean(
        required=True,
        description="A required slot with nothing that fits refuses the container.",
    )
    sort_order = graphene.Int(required=True, description="Lowest first.")
    axes = NonNullList(
        graphene.String,
        required=True,
        description="The questions this slot asks AFTER the vehicle, in order.",
    )
    partitioning_axis = graphene.String(
        required=True,
        description=(
            "The axis that decides which of the fitting candidates the shopper "
            "ends up on. Blank means this slot takes every candidate that fits."
        ),
    )
    miss_message = graphene.String(
        required=True,
        description="What a shopper is told when nothing here fits their vehicle.",
    )
    source_collection = graphene.Field(
        Collection,
        description=(
            "Fill this slot from another container's collection rather than "
            "listing candidates, so a kit can hold a series by reference."
        ),
    )
    candidates = NonNullList(
        lambda: WsmKitMember,
        required=True,
        description="The parts listed for this role, ordered by sort order.",
    )
    drills = graphene.Boolean(
        required=True,
        description=(
            "Whether the shopper picks ONE of the survivors. Read off the data "
            "(a partitioning axis), never off a toggle."
        ),
    )

    class Meta:
        model = models.ContainerSlot
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM
        description = "One named, ordered, quantified role inside a container."

    @staticmethod
    def resolve_axes(root: models.ContainerSlot, _info):
        return list(root.axes or [])

    @staticmethod
    def resolve_source_collection(root: models.ContainerSlot, _info):
        if root.source_collection_id is None:
            return None
        return _collection(root.source_collection)

    @staticmethod
    def resolve_candidates(root: models.ContainerSlot, _info):
        return root.candidates.all()


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
    slot = graphene.Field(
        WsmContainerSlot,
        description=(
            "The role this part fills. Null only on a container whose slots "
            "have not been authored yet."
        ),
    )

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
    discount_amount = WsmDecimal(
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
    slots = NonNullList(
        WsmContainerSlot,
        required=True,
        description="The roles this container is made of, ordered by sort order.",
    )
    brand = graphene.String(
        required=True, description="The one brand this container covers, or blank."
    )
    published = graphene.Boolean(
        description=(
            "Show this container as its own shoppable page. Null means nobody "
            "has answered, and `isPublished` says what that means."
        )
    )
    is_published = graphene.Boolean(
        required=True,
        description=(
            "Whether a shopper can reach this container. Falls back to `active` "
            "for every row written before `published` existed."
        ),
    )
    miss_message = graphene.String(
        required=True,
        description="What a shopper is told when their vehicle leaves a slot empty.",
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
    def resolve_slots(root: models.KitConfig, _info):
        # Unbatched, exactly like `members` and `rules` beside it: the Dashboard
        # list screens select neither, and `test_list_query_cost` holds the bar
        # for the fields they do select. ponytail: the ceiling is a list screen
        # that starts showing slot counts; the upgrade is one dataloader keyed
        # by kit id, next to the two already in `dataloaders.py`.
        return root.slots.all()

    @staticmethod
    def resolve_currency_code(root: models.KitConfig, info):
        """What the members are priced in, read off the members.

        A kit carries no currency column because no WSM column does: the honest
        answer is the one the merchant's own listing gives, and the shop's
        channel is the fallback while a kit is still empty. Batched, because the
        kit LIST selects this field and an inline query there is 1+N.
        """
        from .dataloaders import CurrencyCodeByKitIdLoader

        return CurrencyCodeByKitIdLoader(info.context).load(root.pk)


class WsmSeriesConfigCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmSeriesConfig


class WsmKitConfigCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmKitConfig


# --- what a vehicle resolves this container to --------------------------------


class WsmContainerRefusal(BaseObjectType):
    """Why the container could not be sold, in the merchant's own sentence."""

    code = WsmContainerRefusalCode(
        required=True, description="What a storefront branches on."
    )
    message = graphene.String(
        required=True, description="What the shopper is told, in the merchant's words."
    )
    slot_label = graphene.String(
        required=True, description="The role that could not be filled, if it was one."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM
        description = "A container this vehicle cannot be sold."


class WsmResolvedCandidate(BaseObjectType):
    variant = graphene.Field(
        ProductVariant, required=True, description="The SKU this candidate is."
    )
    member = graphene.Field(
        WsmKitMember,
        description=(
            "The row a merchant typed. Null when the candidate was DERIVED from "
            "the slot's source collection, which is how a kit holds a series."
        ),
    )
    product_id = graphene.ID(
        required=True,
        description="The product the search engine resolved fitment at.",
    )
    quantity = graphene.Int(required=True, description="How many of it.")
    sort_order = graphene.Int(required=True, description="Lowest first.")
    fitment = WsmContainerFitment(
        required=True, description="What the engine says about it for this vehicle."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM
        description = "One thing that could fill a slot on this vehicle."

    @staticmethod
    def resolve_variant(root, _info):
        return ChannelContext(node=root.variant, channel_slug=None)

    @staticmethod
    def resolve_product_id(root, _info):
        return graphene.Node.to_global_id("Product", root.product_id)


class WsmResolvedSlot(BaseObjectType):
    slot = graphene.Field(
        WsmContainerSlot, required=True, description="The role being filled."
    )
    candidates = NonNullList(
        WsmResolvedCandidate,
        required=True,
        description="Everything that survived the vehicle, in slot order.",
    )
    selected = NonNullList(
        WsmResolvedCandidate,
        required=True,
        description=(
            "What goes in the cart with no further input. One survivor in a "
            "drilling slot auto-selects; several select nothing until asked."
        ),
    )
    choose = graphene.Boolean(
        required=True, description="Whether the shopper still has a decision here."
    )
    excluded = graphene.Int(
        required=True,
        description=(
            "How many candidates the vehicle ruled out. Reported rather than "
            "dropped in silence."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM
        description = "One slot, resolved for one vehicle."


class WsmContainerResolution(BaseObjectType):
    kit = graphene.Field(WsmKitConfig, description="The container, or null if none.")
    vehicle = graphene.String(
        required=True, description="The fitment pairs this was resolved for."
    )
    slots = NonNullList(
        WsmResolvedSlot, required=True, description="Empty when the container refused."
    )
    refused = graphene.Boolean(required=True, description="Whether there is a refusal.")
    refusal = graphene.Field(WsmContainerRefusal, description="Why, when there is one.")
    engine_calls = graphene.Int(
        required=True,
        description=(
            "Search-engine round trips this resolve cost: 0 with no vehicle, 2 "
            "with one, and independent of the member count either way."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM
        description = (
            "A container resolved for a vehicle: the assortment, or a refusal."
        )
