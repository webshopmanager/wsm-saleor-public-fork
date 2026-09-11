# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's GraphQL types: the five rows a merchant edits.

Field names are the contract's (`wsm-schema-contract.graphql` section 2), which
are graphene's camel-casing of the model's own columns; nothing is renamed for
taste. Every relation resolves through a dataloader, because the Compose tab
selects three levels deep on a list of products.
"""

import graphene

from ....graphql.core.connection import CountableConnection
from ....graphql.core.context import ChannelContext
from ....graphql.core.scalars import Decimal, PositiveDecimal
from ....graphql.core.types import ModelObjectType, NonNullList
from ....graphql.product.dataloaders import ProductByIdLoader
from ....graphql.product.types import Product, ProductVariant
from ....graphql.shipping.types import ShippingZone
from ...compose import models
from ..dealer.types import WsmDealerGroup
from ..types import DOC_CATEGORY_WSM
from . import dataloaders as loaders
from .enums import WsmFeeBasisEnum, WsmFeeScopeEnum, WsmOptionSetPromptTypeEnum


def _in_context(node):
    """Stock's Product, ProductVariant and ShippingZone are channel-context types.

    Their resolvers read `root.node`, so handing one a bare model instance is an
    AttributeError on the first field selected under it. A compose row belongs
    to no channel, so the context carries no slug: the Dashboard reads the name
    and the id off it, and the channel-dependent fields underneath stay null
    rather than guessing a channel this row never had. Same shape stock uses
    when it has no channel either (`menu_create.py:123`).
    """
    return ChannelContext(node=node, channel_slug=None)


class WsmDealerTierOptionPrice(ModelObjectType[models.DealerTierOptionPrice]):
    id = graphene.GlobalID(required=True, description="ID of the dealer delta row.")
    option_value = graphene.Field(
        lambda: WsmOptionValue,
        required=True,
        description="The choice this group is being priced on.",
    )
    dealer_group = graphene.Field(
        WsmDealerGroup,
        description="The resolved group row, null if the code names a deleted group.",
    )
    tier_group = graphene.String(
        required=True,
        description=(
            "The DealerGroup code this price is for, as a plain string and NOT a "
            "global ID: the column is a CharField on purpose, so compose can "
            "price without joining wsm_dealer."
        ),
    )
    price_delta = Decimal(
        required=True,
        description=(
            "Signed, and never above the retail priceDelta floored at zero. A "
            "credit is negative."
        ),
    )

    class Meta:
        description = "One buyer group's delta for one choice."
        model = models.DealerTierOptionPrice
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_option_value(root, info):
        return loaders.OptionValueByIdLoader(info.context).load(root.option_value_id)

    @staticmethod
    def resolve_dealer_group(root, info):
        # Imported here, not at module scope: `dealer/types.py` is another
        # unit's file and a top-level import would make compose unimportable
        # while that unit is mid-flight. The field is in the contract, so the
        # dependency is real; the import site is where it costs least.
        from ..dealer.dataloaders import DealerGroupByCodeLoader

        return DealerGroupByCodeLoader(info.context).load(root.tier_group)


class WsmOptionValue(ModelObjectType[models.OptionValue]):
    id = graphene.GlobalID(required=True, description="ID of the choice.")
    option_set = graphene.Field(
        lambda: WsmOptionSet,
        required=True,
        description="The question this is an answer to.",
    )
    name = graphene.String(
        required=True, description="What the shopper sees for this choice."
    )
    sku_fragment = graphene.String(
        required=True,
        description=(
            "Appended to the product SKU when this choice is picked. Empty "
            "string allowed."
        ),
    )
    price_delta = Decimal(
        required=True,
        description=(
            "Signed. Decimal, not PositiveDecimal: a credit is a negative delta."
        ),
    )
    image_url = graphene.String(required=True, description="Swatch or thumbnail URL.")
    sort_order = graphene.Int(required=True, description="Low numbers first.")
    tier_deltas = NonNullList(
        WsmDealerTierOptionPrice,
        required=True,
        description="Ordered by tierGroup then pk. One query per request, never N+1.",
    )

    class Meta:
        description = "One answer to one question."
        model = models.OptionValue
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_option_set(root, info):
        return loaders.OptionSetByIdLoader(info.context).load(root.option_set_id)

    @staticmethod
    def resolve_tier_deltas(root, info):
        return loaders.TierDeltasByOptionValueIdLoader(info.context).load(root.id)


class WsmOptionSet(ModelObjectType[models.OptionSet]):
    id = graphene.GlobalID(required=True, description="ID of the question.")
    product = graphene.Field(
        Product, required=True, description="The product that asks this question."
    )
    name = graphene.String(
        required=True,
        description="The merchant's internal name. Shown only when label is empty.",
    )
    label = graphene.String(
        required=True, description="What the shopper sees. Empty string, never null."
    )
    prompt_type = WsmOptionSetPromptTypeEnum(
        required=True, description="How the shopper answers."
    )
    required = graphene.Boolean(
        required=True, description="The shopper cannot add to cart without answering."
    )
    note = graphene.String(
        required=True,
        description="Help shown to the shopper. Empty string, never null.",
    )
    sort_order = graphene.Int(required=True, description="Low numbers first.")
    values = NonNullList(
        WsmOptionValue,
        required=True,
        description="Ordered by sortOrder then pk, the model's own Meta.ordering.",
    )
    currency_code = graphene.String(
        description=(
            "Currency of the product's cheapest channel listing, for labelling "
            "money inputs. Null when the product is in no channel."
        )
    )

    class Meta:
        description = "One question a product asks, and the answers it accepts."
        model = models.OptionSet
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_product(root, info):
        return ProductByIdLoader(info.context).load(root.product_id).then(_in_context)

    @staticmethod
    def resolve_values(root, info):
        return loaders.OptionValuesByOptionSetIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_currency_code(root, info):
        return loaders.CurrencyByProductIdLoader(info.context).load(root.product_id)


class WsmFee(ModelObjectType[models.Fee]):
    id = graphene.GlobalID(required=True, description="ID of the charge.")
    product = graphene.Field(
        Product, required=True, description="The product this charge is attached to."
    )
    label = graphene.String(required=True, description="What the shopper sees.")
    sku = graphene.String(required=True, description="The merchant's own code.")
    basis = WsmFeeBasisEnum(required=True, description="Flat amount or percentage.")
    amount = PositiveDecimal(
        required=True,
        description=(
            "Money when basis is FIXED, a percentage when it is PERCENT (8.25 "
            "means 8.25%). One column, two meanings."
        ),
    )
    apply_to = WsmFeeScopeEnum(
        required=True, description="Charged per item, or once per line."
    )
    required = graphene.Boolean(required=True, description="Always charged.")
    decline_label = graphene.String(
        required=True, description="Wording of the decline option, when declinable."
    )
    variant = graphene.Field(
        ProductVariant,
        description=(
            "Read-only. The hidden carrier variant, created server-side the "
            "first time the charge is bought. Never accepted on input."
        ),
    )
    currency_code = graphene.String(
        description="Currency of the product's cheapest channel listing."
    )

    class Meta:
        description = "A charge attached to a product: crating, oversize, hazmat."
        model = models.Fee
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_product(root, info):
        return ProductByIdLoader(info.context).load(root.product_id).then(_in_context)

    @staticmethod
    def resolve_variant(root, info):
        if root.variant_id is None:
            return None
        from ....graphql.product.dataloaders import ProductVariantByIdLoader

        return (
            ProductVariantByIdLoader(info.context)
            .load(root.variant_id)
            .then(_in_context)
        )

    @staticmethod
    def resolve_currency_code(root, info):
        return loaders.CurrencyByProductIdLoader(info.context).load(root.product_id)


class WsmProductCompliance(ModelObjectType[models.ProductCompliance]):
    id = graphene.GlobalID(required=True, description="ID of the compliance row.")
    product = graphene.Field(
        Product, required=True, description="The product this row belongs to."
    )
    prop65 = graphene.Boolean(
        required=True, description="Show the California Proposition 65 warning."
    )
    prop65_text = graphene.String(
        required=True,
        description="Empty string means the storefront's standard short-form warning.",
    )
    restricted_states = NonNullList(
        graphene.String,
        required=True,
        description=(
            "Stored as one comma-separated column and exposed as the normalised "
            "list. Empty list restricts nothing."
        ),
    )
    include_shipping_zones = NonNullList(
        ShippingZone,
        required=True,
        description=(
            "Naming zones means this product ships ONLY to the countries they cover."
        ),
    )
    restriction_message = graphene.String(
        required=True,
        description="What the shopper is told when a destination is refused.",
    )

    class Meta:
        description = "What a product must say and where it may not go."
        model = models.ProductCompliance
        interfaces = [graphene.relay.Node]
        doc_category = DOC_CATEGORY_WSM

    @staticmethod
    def resolve_product(root, info):
        return ProductByIdLoader(info.context).load(root.product_id).then(_in_context)

    @staticmethod
    def resolve_restricted_states(root, info):
        # The model's own normaliser, so the list the Dashboard renders is the
        # list the checkout matcher compares against, character for character.
        return list(root.state_codes)

    @staticmethod
    def resolve_include_shipping_zones(root, info):
        from ....graphql.shipping.dataloaders import ShippingZoneByIdLoader

        def load_zones(zone_ids):
            if not zone_ids:
                return []
            return (
                ShippingZoneByIdLoader(info.context)
                .load_many(zone_ids)
                .then(lambda zones: [_in_context(zone) for zone in zones])
            )

        return (
            loaders.ShippingZoneIdsByComplianceIdLoader(info.context)
            .load(root.id)
            .then(load_zones)
        )


class WsmOptionSetCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmOptionSet


class WsmFeeCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmFee


class WsmProductComplianceCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        node = WsmProductCompliance
