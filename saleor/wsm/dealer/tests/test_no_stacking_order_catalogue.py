# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""The same promotion, taken once, after the checkout has become an order.

`test_no_stacking_sale_price.py` closed this on the CHECKOUT path: a kit member
or a configured line is priced by the fork FROM the listing's sale price, so the
promotion is already inside `price_override`, and stock Saleor took the same
rule off that number a second time. The order path has its own copy of that
code, `saleor/discount/utils/order.py`, and it was never wrapped, so every
recalculation an order gets after completion (a draft order the merchant builds
or edits, a line whose frozen draft price has expired) puts the rule back on:
the Alba kit member at 1899.99 falls to 1645.98 again.

`create_order_from_checkout` copies `price_override_reason` and the private
metadata stamps onto the order line, so the same three predicates that decide
the question on a checkout line answer it on an order line with no new field and
no new rule: `is_sale_priced_line`, `is_dealer_line`, `is_fee_line`. The order
guard therefore passes the SAME `also_excluded` predicate to the SAME
`split_discountable` the checkout guard uses. If the two halves ever disagree, a
shopper is charged one number in the cart and a different one on the order.
"""

import json
from decimal import Decimal

import graphene
import pytest
from prices import Money, TaxedMoney

from ....discount import DiscountType, DiscountValueType, RewardValueType
from ....discount.models import OrderLineDiscount, Promotion
from ....discount.utils.order import (
    refresh_order_line_discount_objects_for_catalogue_promotions,
)
from ....order import OrderStatus
from ....order.fetch import fetch_draft_order_lines_info
from ....order.models import OrderLine
from ....product.models import ProductVariant, ProductVariantChannelListing

pytestmark = pytest.mark.django_db

USD = "USD"
# The live numbers off the Alba kit member, to the cent.
LIST_UNIT = Decimal("2154.00")
REWARD = Decimal("254.01")
SALE_UNIT = LIST_UNIT - REWARD  # 1899.99
# The dealer tier and the crate charge are prices beside the catalogue, listed
# under the same rule so that nothing but the guard keeps the rule off them.
DEALER_LIST = Decimal("1700.00")
DEALER_UNIT = DEALER_LIST - REWARD
FEE_LIST = Decimal("500.00")
FEE_UNIT = FEE_LIST - REWARD

KIT_REASON = "wsm.containers"
COMPOSE_REASON = "wsm.compose"
DEALER_REASON = "wsm.dealer"
META_KIT = "wsm.kit"
META_OPTIONS = "wsm.options"
META_DEALER = "wsm.dealer"
META_FEE = "compose.fee"

KIT = "350-96MM-KIT"
CONFIGURED = "CONFIGURED-01"
PLAIN = "PLAIN-01"
DEALER = "DEALER-01"
FEE = "CRATE-01"


def _listed(variant, channel, rule, price):
    """List the variant, on sale for `price` less the one 254.01 rule."""
    listing = ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel,
        price_amount=price,
        discounted_price_amount=price - REWARD,
        currency=channel.currency_code,
    )
    listing.variantlistingpromotionrule.create(
        promotion_rule=rule,
        discount_amount=REWARD,
        currency=channel.currency_code,
    )
    return listing


@pytest.fixture
def on_sale(product, channel_USD):
    """One product, five variants, one 254.01 catalogue rule over the product.

    One rule and one product on purpose: the merchant did not write five sales,
    they wrote one, and the five lines differ only in how the fork priced them.
    """
    promotion = Promotion.objects.create(name="Alba rebuild kits")
    rule = promotion.rules.create(
        catalogue_predicate={
            "productPredicate": {
                "ids": [graphene.Node.to_global_id("Product", product.pk)]
            }
        },
        reward_value_type=RewardValueType.FIXED,
        reward_value=REWARD,
    )
    rule.channels.add(channel_USD)
    variants = {}
    for sku, price in (
        (KIT, LIST_UNIT),
        (CONFIGURED, LIST_UNIT),
        (PLAIN, LIST_UNIT),
        (DEALER, DEALER_LIST),
        (FEE, FEE_LIST),
    ):
        variant = ProductVariant.objects.create(
            product=product, sku=sku, track_inventory=False
        )
        _listed(variant, channel_USD, rule, price)
        variants[sku] = variant
    return rule, variants


@pytest.fixture
def placed(order, channel_USD, on_sale):
    """The five lines as `create_order_from_checkout` leaves them on the order.

    `undiscounted_base_unit_price` is the price the fork wrote, not the listing
    price, because `calculate_undiscounted_base_line_unit_price` reads
    `CheckoutLineInfo.undiscounted_unit_price`, which returns the override
    before any promotion. That is the number the merchant agreed to charge, and
    the number this file is about.
    """
    rule, variants = on_sale
    order.channel = channel_USD
    order.status = OrderStatus.DRAFT
    order.save(update_fields=["channel", "status"])

    def line(sku, unit, reason, stamps):
        variant = variants[sku]
        money = Money(unit, USD)
        return OrderLine.objects.create(
            order=order,
            product_name=str(variant.product),
            variant_name=str(variant),
            product_sku=variant.sku,
            product_variant_id=variant.get_global_id(),
            is_shipping_required=True,
            is_gift_card=False,
            quantity=1,
            variant=variant,
            currency=USD,
            unit_price=TaxedMoney(net=money, gross=money),
            total_price=TaxedMoney(net=money, gross=money),
            undiscounted_unit_price=TaxedMoney(net=money, gross=money),
            undiscounted_total_price=TaxedMoney(net=money, gross=money),
            base_unit_price=money,
            undiscounted_base_unit_price=money,
            tax_rate=Decimal("0.00"),
            is_price_overridden=reason is not None,
            price_override_reason=reason,
            private_metadata=stamps,
        )

    lines = {
        KIT: line(
            KIT, SALE_UNIT, KIT_REASON,
            {META_KIT: json.dumps({"collection": 3, "quantity": 1})},
        ),
        CONFIGURED: line(
            CONFIGURED, SALE_UNIT, COMPOSE_REASON,
            {META_OPTIONS: json.dumps({"1": "2"})},
        ),
        PLAIN: line(PLAIN, LIST_UNIT, None, {}),
        DEALER: line(
            DEALER, DEALER_UNIT, DEALER_REASON,
            {META_DEALER: json.dumps({"group": 1, "break": 1})},
        ),
        FEE: line(
            FEE, FEE_LIST, COMPOSE_REASON,
            {META_FEE: json.dumps({"fee": 1}), META_OPTIONS: json.dumps({})},
        ),
    }
    return rule, lines


def _recalculated(order):
    """Run the real entry point, then read the prices it left behind.

    `refresh_order_line_discount_objects_for_catalogue_promotions` is the order
    path's whole catalogue step: it derives the discount rows, writes them, and
    re-derives every line's base unit price from them. Its one caller,
    `refresh_order_base_prices_and_discounts`, saves the lines afterwards, which
    is the `bulk_update` here.
    """
    lines_info = fetch_draft_order_lines_info(order, fetch_actual_prices=True)
    refresh_order_line_discount_objects_for_catalogue_promotions(lines_info)
    OrderLine.objects.bulk_update(
        [info.line for info in lines_info], ["base_unit_price_amount"]
    )
    return {info.line.product_sku: info for info in lines_info}


def _charged(line):
    line.refresh_from_db()
    return line.base_unit_price


def test_a_kit_member_order_line_is_charged_the_sale_price_once(placed, order):
    """1899.99, not 1645.98. The live defect, on the order side of the seam."""
    _rule, lines = placed

    _recalculated(order)

    assert _charged(lines[KIT]) == Money(SALE_UNIT, USD)
    assert not OrderLineDiscount.objects.filter(
        line=lines[KIT], type=DiscountType.PROMOTION
    ).exists()


def test_a_configured_order_line_is_charged_the_sale_price_once(placed, order):
    """The compose half of the same rule, priced by the same `unit_amount`."""
    _rule, lines = placed

    _recalculated(order)

    assert _charged(lines[CONFIGURED]) == Money(SALE_UNIT, USD)
    assert not OrderLineDiscount.objects.filter(
        line=lines[CONFIGURED], type=DiscountType.PROMOTION
    ).exists()


def test_a_plain_order_line_beside_them_still_gets_the_promotion(placed, order):
    """The guard that removed this row would be worse than the defect it fixes.

    Same order, same rule, same product: the only line the fork did not price is
    the only line stock Saleor is still allowed to discount, from 2154.00 to
    1899.99, and it keeps its discount row.
    """
    _rule, lines = placed

    _recalculated(order)

    assert _charged(lines[PLAIN]) == Money(SALE_UNIT, USD)
    assert (
        OrderLineDiscount.objects.filter(
            line=lines[PLAIN], type=DiscountType.PROMOTION
        ).count()
        == 1
    )


def test_a_dealer_order_line_keeps_its_tier_price(placed, order):
    """The checkout guard drops a dealer line while stacking is off; so does this.

    The tier is a price beside the catalogue rather than one derived from it, so
    it is excluded by `is_dealer_line` under the merchant's own toggle, not by
    the sale-price rule. Both halves have to agree or the cart and the order
    quote two different numbers.
    """
    _rule, lines = placed

    _recalculated(order)

    assert _charged(lines[DEALER]) == Money(DEALER_UNIT, USD)
    assert not OrderLineDiscount.objects.filter(line=lines[DEALER]).exists()


def test_a_fee_order_line_is_never_discounted(placed, order):
    """A crating charge is money owed to somebody else, on the order too."""
    _rule, lines = placed

    _recalculated(order)

    assert _charged(lines[FEE]) == Money(FEE_LIST, USD)
    assert not OrderLineDiscount.objects.filter(line=lines[FEE]).exists()


def test_a_promotion_that_landed_before_the_order_line_was_ours_comes_back_off(
    placed, order, channel_USD
):
    """The stale-row half, which the checkout guard already relies on.

    A merchant can put a product on sale after the order was placed. The row
    written while the line was plain has to come off on the next recalculation,
    or the rule only holds for lines placed after the sale.
    """
    rule, lines = placed
    OrderLineDiscount.objects.create(
        line=lines[KIT],
        type=DiscountType.PROMOTION,
        value_type=DiscountValueType.FIXED,
        value=REWARD,
        amount_value=REWARD,
        currency=channel_USD.currency_code,
        promotion_rule=rule,
        unique_type=DiscountType.PROMOTION,
    )

    _recalculated(order)

    assert not OrderLineDiscount.objects.filter(line=lines[KIT]).exists()
    assert _charged(lines[KIT]) == Money(SALE_UNIT, USD)


def test_the_whole_order_charges_each_sale_once(placed, order):
    """The subtotal a merchant reads, over one rule and five lines."""
    _rule, lines = placed

    _recalculated(order)

    subtotal = sum(
        (_charged(line) * line.quantity for line in lines.values()),
        Money(0, USD),
    )
    assert subtotal == Money(
        SALE_UNIT + SALE_UNIT + SALE_UNIT + DEALER_UNIT + FEE_LIST, USD
    )
