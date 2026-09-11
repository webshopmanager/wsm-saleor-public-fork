# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's GraphQL types: the group, who is in it, what it pays.

One buyer-group CODE names a group across three places: the tier rows here, the
option-value deltas in wsm.compose, and the storefront. `WsmDealerGroup.code` is
that string, and it is why the compose screens ask this domain for a picker list
rather than keeping a second copy of the names.

The group carries COUNTS rather than lists, for the reason the admin showed an
aggregate instead of an inline (`dealer/admin.py:32`): live data is 304+ tier
rows on one group, so a list screen that embedded them would fetch a spreadsheet
to render a row.
"""

import graphene

from ....graphql.account.types import User
from ....graphql.core.connection import CountableConnection
from ....graphql.core.context import ChannelContext
from ....graphql.core.types import BaseObjectType, ModelObjectType
from ....graphql.product.types.products import ProductVariant
from ...dealer import models
from ..scalars import WsmDecimal
from ..types import DOC_CATEGORY_WSM, WsmDocCategory

# The names the list resolvers annotate their counts under. Read off the row
# when it is there and counted per row when it is not, because a mutation
# payload hands back a plain instance that no annotation ever touched.
TIER_PRICE_COUNT = "wsm_tier_price_count"
CUSTOMER_COUNT = "wsm_customer_count"


class WsmDealerGroup(WsmDocCategory, ModelObjectType[models.DealerGroup]):
    id = graphene.GlobalID(required=True, description="ID of the dealer group.")
    code = graphene.String(
        required=True,
        description=(
            "The exact code that names this buyer group everywhere else: on the "
            "dealer tier rows of an option value, and in the storefront. Unique, "
            "and never changed once prices point at it."
        ),
    )
    name = graphene.String(
        required=True, description="What staff see. Empty means they see the code."
    )
    tier_price_count = graphene.Int(
        required=True, description="How many tier prices this group carries."
    )
    customer_count = graphene.Int(
        required=True, description="How many shoppers buy at this group's prices."
    )

    class Meta:
        model = models.DealerGroup
        interfaces = [graphene.relay.Node]
        description = "A buyer group: dealer-1, warehouse, installer."

    @staticmethod
    def resolve_tier_price_count(root: models.DealerGroup, _info):
        count = getattr(root, TIER_PRICE_COUNT, None)
        return root.tier_prices.count() if count is None else count

    @staticmethod
    def resolve_customer_count(root: models.DealerGroup, _info):
        count = getattr(root, CUSTOMER_COUNT, None)
        return root.customers.count() if count is None else count


class WsmDealerAccountStatus(graphene.Enum):
    """Whether this account may place orders at all.

    Declared here rather than derived from the model's choices because a
    graphene enum built from a Django `choices` list names its members after
    the stored string, and the merchant screen and the storefront both read
    these names.
    """

    ACTIVE = models.ACCOUNT_STATUS_ACTIVE
    HOLD = models.ACCOUNT_STATUS_HOLD

    @property
    def description(self):
        if self == WsmDealerAccountStatus.HOLD:
            return "On hold: this shopper can place no order, by any method."
        return "Active: this shopper can place orders."


WsmDealerAccountStatus.doc_category = DOC_CATEGORY_WSM


class WsmDealerCustomer(WsmDocCategory, ModelObjectType[models.DealerCustomer]):
    id = graphene.GlobalID(required=True, description="ID of the assignment.")
    user = graphene.Field(
        User, required=True, description="The signed-in shopper this is about."
    )
    group = graphene.Field(
        WsmDealerGroup,
        required=True,
        description="The one buyer group whose prices this shopper gets.",
    )
    tax_exempt = graphene.Boolean(
        required=True,
        description=(
            "Charge this shopper no sales tax. Their exemption certificate is "
            "the merchant's to hold on file; nothing here checks for one."
        ),
    )
    invoice_payment = graphene.Boolean(
        required=True,
        description=(
            "This shopper may place orders without paying, to be invoiced. A "
            "payment method the account enables alongside dealer pricing, never "
            "instead of it: the lines still carry their dealer prices."
        ),
    )
    account_number = graphene.String(
        required=True,
        description=(
            "This dealer's account number in the merchant's own books or ERP. "
            "Empty when they have not given one."
        ),
    )
    account_status = WsmDealerAccountStatus(
        required=True,
        description="Whether this account may place orders at all.",
    )

    class Meta:
        model = models.DealerCustomer
        interfaces = [graphene.relay.Node]
        description = "The link from a signed-in shopper to their buyer group."


class WsmMyDealerTerms(WsmDocCategory, BaseObjectType):
    """What the SIGNED-IN shopper is allowed to do, answered to themselves.

    The storefront has to know three things before it can draw a payment step:
    whether to offer paying on account at all, whether to demand a PO number,
    and whether this account is on hold. Every other dealer query in this layer
    is behind MANAGE_DISCOUNTS, because every other one answers about SOMEBODY
    ELSE; this one answers only about the caller, so the gate is the session and
    there is nothing to authorize.

    A plain `ObjectType` rather than a `ModelObjectType` on `DealerCustomer`, on
    purpose: a model type carries a Node id and a `group`, and a shopper has no
    business resolving another shopper's assignment from an id they guessed.
    Four scalars, and nothing to walk from.

    Null for a shopper who is not signed in and for one with no dealer record,
    which are the same answer to the only question being asked.
    """

    invoice_payment = graphene.Boolean(
        required=True,
        description="Whether this shopper may place an order to be invoiced.",
    )
    account_number = graphene.String(
        required=True,
        description="Their account number with the merchant. Empty when unset.",
    )
    account_status = WsmDealerAccountStatus(
        required=True,
        description=(
            "HOLD means no order can be placed by any payment method, so the "
            "storefront says so at the address step rather than at the end."
        ),
    )
    po_required = graphene.Boolean(
        required=True,
        description=(
            "Whether the merchant demands a purchase order number on an account "
            "order. The store-wide setting, repeated here so a checkout needs "
            "one round trip rather than two."
        ),
    )

    class Meta:
        description = "What the signed-in shopper's dealer account allows."


class WsmTierPrice(WsmDocCategory, ModelObjectType[models.TierPrice]):
    id = graphene.GlobalID(required=True, description="ID of the tier price.")
    variant = graphene.Field(
        ProductVariant, required=True, description="The exact SKU this price is for."
    )
    group = graphene.Field(
        WsmDealerGroup, required=True, description="The group that pays this price."
    )
    min_quantity = graphene.Int(
        required=True,
        description="This price applies from this quantity up, until the next break.",
    )
    amount = WsmDecimal(
        required=True,
        description=(
            "What this group pays EACH, and at least one cent. An absolute "
            "price, never a discount off retail. The column holds THREE decimal "
            "places to match CheckoutLine.price_override; the merchant screen "
            "takes two, as the admin form did."
        ),
    )
    currency_code = graphene.String(
        description=(
            "What the SKU is priced in, so a merchant screen labels the money "
            "box without a second round trip."
        )
    )

    class Meta:
        model = models.TierPrice
        interfaces = [graphene.relay.Node]
        description = "One quantity break: what one group pays for one SKU."

    @staticmethod
    def resolve_variant(root: models.TierPrice, _info):
        # `ProductVariant` is a `ChannelContextType`, so its default resolver
        # reads `root.node`: a bare model instance renders every field as null.
        # No channel, because a merchant screen is not shopping in one.
        return ChannelContext(node=root.variant, channel_slug=None)

    @staticmethod
    def resolve_currency_code(root: models.TierPrice, _info):
        """The cheapest of the SKU's own listings, or nothing.

        Cheapest rather than "whichever row the database returned first": the
        merchant screen labels ONE money box, and an unordered pick labels it
        differently between two requests for no reason the merchant can see.
        Null when the SKU is in no channel, which is the honest answer and what
        the field being nullable is for; the fallback this replaces took an
        arbitrary Channel's currency and labelled the box with a currency this
        SKU is not sold in.

        A 300-row grid is the screen this field exists for, so it must not cost
        a query per row: every resolver that hands back tier prices prefetches
        `variant__channel_listings`, and `.all()` on a prefetched manager reads
        the cache. Ordered in python for the same reason, because `order_by` on
        a prefetched manager is a fresh query per row.
        ponytail: the ceiling is a caller that reaches a tier price WITHOUT that
        prefetch (a mutation payload, one row), which pays one query. The
        upgrade, if a second unprefetched list ever appears, is a dataloader.
        """
        priced = [
            listing
            for listing in root.variant.channel_listings.all()
            if listing.price_amount is not None
        ]
        if not priced:
            return None
        return min(priced, key=lambda listing: listing.price_amount).currency


class WsmDealerSettings(WsmDocCategory, ModelObjectType[models.DealerSettings]):
    """The dealer-pricing toggles, as one object.

    No `id` and no `Node` interface on purpose: the table holds one row or none,
    so there is nothing to look up by id and nothing to page through. The
    resolver hands back an unsaved instance when the row does not exist yet, so
    a merchant screen reads the DEFAULTS rather than a null, which is what the
    Django admin did by creating the row on first visit.
    """

    id = graphene.ID(
        description=(
            "Null until the row is first written. A written-yet marker, not a "
            "lookup key: nothing resolves this back, because the store is the "
            "key and there is only ever one row."
        )
    )
    discount_stacking = graphene.Boolean(
        required=True,
        description=(
            "When false (the default), a line already at a dealer price takes "
            "no voucher, promotion or order-level discount on top."
        ),
    )
    po_required = graphene.Boolean(
        required=True,
        description=(
            "When true, a shopper paying on account must give a purchase order "
            "number before the order is placed. False (the default) still "
            "offers and stores the box, it just does not demand it."
        ),
    )

    class Meta:
        model = models.DealerSettings
        description = "Store-wide dealer pricing settings."

    @staticmethod
    def resolve_id(root: models.DealerSettings, _info):
        if root.pk is None:
            return None
        return graphene.Node.to_global_id("WsmDealerSettings", root.pk)


class WsmDealerGroupCountableConnection(WsmDocCategory, CountableConnection):
    class Meta:
        node = WsmDealerGroup


class WsmDealerCustomerCountableConnection(WsmDocCategory, CountableConnection):
    class Meta:
        node = WsmDealerCustomer


class WsmTierPriceCountableConnection(WsmDocCategory, CountableConnection):
    class Meta:
        node = WsmTierPrice
