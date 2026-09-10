# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""A promotion the fork already took is not taken a second time by stock Saleor.

The defect these close, measured live on the Tonneau Outlaw stage 2026-09-09.
The Alba RZR900 kit (Collection:3, member variant 33900217, sku 350-96MM-KIT) is
listed at 2154.00 with a 254.01 catalogue promotion on it, so the merchant's sale
price is 1899.99. Since commit ede3c010 the kit endpoint prices its member lines
from `money.unit_amount`, which answers with the SALE price, and writes 1899.99
to `CheckoutLine.price_override`. Stock Saleor then reads that override and
applies the same 254.01 rule to it again, by design
(`saleor/discount/utils/promotion.py`, `_get_discount_amount`): the member line
billed at 1645.98 and the four-line kit at a subtotal of 2278.98 instead of
2532.99. The merchant put one sale on the product and the shopper took it twice.

The rule is the reason column, not a new field: `wsm.containers` on a kit member
and `wsm.compose` on a configured line are exactly the two prices the fork
derived from the sale price, and both are already written on every such line.

What is deliberately NOT here:

- A plain variant line under the same promotion still gets it. The guard removes
  a discount only from the lines the fork priced, and the third test in this file
  is the one that catches a guard that got greedy.
- A voucher still stacks. `unit_amount` reads a catalogue promotion and nothing
  else, so a voucher is a discount that has not been counted yet.
- A dealer line is unchanged. Its tier is a price beside the catalogue, not one
  derived from it, and it leaves the eligible set through `is_dealer_line`.
"""

import json
from decimal import Decimal

import graphene
import pytest
from prices import Money

from ....checkout.base_calculations import (
    base_checkout_subtotal,
    calculate_base_line_unit_price,
    calculate_undiscounted_base_line_unit_price,
)
from ....checkout.fetch import fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....discount import DiscountType, DiscountValueType, RewardValueType
from ....discount.models import CheckoutLineDiscount, Promotion
from ....discount.utils.checkout import (
    create_checkout_line_discount_objects_for_catalogue_promotions,
)
from ....product.models import ProductVariant, ProductVariantChannelListing
from ..no_stacking import SALE_PRICED_REASONS, is_sale_priced_line

pytestmark = pytest.mark.django_db

USD = "USD"
# The live numbers off the Alba kit member, to the cent.
LIST_UNIT = Decimal("2154.00")
REWARD = Decimal("254.01")
SALE_UNIT = LIST_UNIT - REWARD  # 1899.99

KIT_REASON = "wsm.containers"
COMPOSE_REASON = "wsm.compose"
META_KIT = "wsm.kit"
META_OPTIONS = "wsm.options"


def test_the_sale_priced_reasons_are_the_ones_the_fork_writes():
    """The literals are repeated to break an import cycle, so they are pinned.

    If either spelling drifts, the line stops being recognised and every kit
    member and configured line in the fleet takes its own promotion twice again,
    silently and only on products the merchant put on sale.
    """
    from ...compose.lines import PRICE_OVERRIDE_REASON as compose_reason
    from ...containers.pricing import PRICE_OVERRIDE_REASON as kit_reason

    assert SALE_PRICED_REASONS == {kit_reason, compose_reason}
    assert (kit_reason, compose_reason) == (KIT_REASON, COMPOSE_REASON)


def _listed(variant, channel, rule=None):
    """List the variant at 2154.00 on sale for 1899.99, the live shape."""
    listing = ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel,
        price_amount=LIST_UNIT,
        discounted_price_amount=SALE_UNIT,
        currency=channel.currency_code,
    )
    if rule is not None:
        listing.variantlistingpromotionrule.create(
            promotion_rule=rule,
            discount_amount=REWARD,
            currency=channel.currency_code,
        )
    return listing


@pytest.fixture
def on_sale(product, channel_USD):
    """One product, three variants, one 254.01 catalogue rule over the product.

    One rule and one product on purpose: the merchant did not write three sales,
    they wrote one, and the three lines differ only in how the fork priced them.
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
    for sku in ("350-96MM-KIT", "CONFIGURED-01", "PLAIN-01"):
        variant = ProductVariant.objects.create(
            product=product, sku=sku, track_inventory=False
        )
        _listed(variant, channel_USD, rule)
        variants[sku] = variant
    return rule, variants


@pytest.fixture
def cart(checkout, channel_USD, on_sale):
    """The three lines in one checkout, priced the way each endpoint prices it.

    The kit member and the configured line carry the override the fork wrote
    from the sale price, with the private stamp and the reason their endpoints
    write beside it. The plain line carries no override, which is how stock
    Saleor prices any variant.
    """
    rule, variants = on_sale

    def line(sku, override, reason, stamps):
        return CheckoutLine.objects.create(
            checkout=checkout,
            variant=variants[sku],
            quantity=1,
            currency=channel_USD.currency_code,
            price_override=override,
            price_override_reason=reason,
            undiscounted_unit_price_amount=LIST_UNIT,
            private_metadata=stamps,
        )

    kit_line = line(
        "350-96MM-KIT",
        SALE_UNIT,
        KIT_REASON,
        {META_KIT: json.dumps({"collection": 3, "quantity": 1})},
    )
    configured_line = line(
        "CONFIGURED-01",
        SALE_UNIT,
        COMPOSE_REASON,
        {META_OPTIONS: json.dumps({"1": "2"})},
    )
    plain_line = line("PLAIN-01", None, None, {})
    return rule, kit_line, configured_line, plain_line


def _priced(checkout):
    """Run the real entry point, then read the prices it left behind."""
    lines_info, _ = fetch_checkout_lines(checkout)
    create_checkout_line_discount_objects_for_catalogue_promotions(lines_info)
    # Re-fetched because CheckoutLineInfo memoises the prices it derives.
    lines_info, _ = fetch_checkout_lines(checkout)
    return lines_info


def _unit(lines_info, line):
    for info in lines_info:
        if info.line.pk == line.pk:
            return info
    raise AssertionError(f"line {line.pk} is not in the checkout")


def test_a_kit_member_line_is_charged_the_sale_price_once(cart, checkout, channel_USD):
    """1899.99, not 1645.98. The live defect, at the size it was measured."""
    _rule, kit_line, _configured, _plain = cart

    info = _unit(_priced(checkout), kit_line)

    assert calculate_base_line_unit_price(info) == Money(SALE_UNIT, USD)
    assert calculate_undiscounted_base_line_unit_price(
        info, channel_USD
    ) == Money(SALE_UNIT, USD)
    assert not CheckoutLineDiscount.objects.filter(
        line=kit_line, type=DiscountType.PROMOTION
    ).exists()


def test_a_configured_line_is_charged_the_sale_price_once(cart, checkout, channel_USD):
    """The compose half of the same rule, priced by the same `unit_amount`."""
    _rule, _kit, configured_line, _plain = cart

    info = _unit(_priced(checkout), configured_line)

    assert calculate_base_line_unit_price(info) == Money(SALE_UNIT, USD)
    assert calculate_undiscounted_base_line_unit_price(
        info, channel_USD
    ) == Money(SALE_UNIT, USD)
    assert not CheckoutLineDiscount.objects.filter(
        line=configured_line, type=DiscountType.PROMOTION
    ).exists()


def test_a_plain_line_beside_them_still_gets_the_promotion(cart, checkout, channel_USD):
    """The guard that removed this row would be worse than the defect it fixes.

    Same checkout, same rule, same product: the only line the fork did not price
    is the only line stock Saleor is still allowed to discount, from 2154.00 to
    1899.99, and it keeps its discount row.
    """
    _rule, _kit, _configured, plain_line = cart

    lines_info = _priced(checkout)
    info = _unit(lines_info, plain_line)

    assert calculate_undiscounted_base_line_unit_price(
        info, channel_USD
    ) == Money(LIST_UNIT, USD)
    assert calculate_base_line_unit_price(info) == Money(SALE_UNIT, USD)
    assert (
        CheckoutLineDiscount.objects.filter(
            line=plain_line, type=DiscountType.PROMOTION
        ).count()
        == 1
    )
    # And the whole cart: three lines on one sale, each charged 1899.99 once.
    assert base_checkout_subtotal(lines_info, channel_USD, USD) == Money(
        SALE_UNIT * 3, USD
    )


def test_a_promotion_that_landed_before_the_line_was_ours_comes_back_off(
    cart, checkout, channel_USD
):
    """The stale-row half, which the fee rule already relies on.

    A merchant can put a product on sale after the kit line was added. The row
    written while the line was plain has to be removed on the next
    recalculation, or the rule only holds for lines added after the sale.
    """
    rule, kit_line, _configured, _plain = cart
    CheckoutLineDiscount.objects.create(
        line=kit_line,
        type=DiscountType.PROMOTION,
        value_type=DiscountValueType.FIXED,
        value=REWARD,
        amount_value=REWARD,
        currency=channel_USD.currency_code,
        promotion_rule=rule,
    )

    info = _unit(_priced(checkout), kit_line)

    assert not CheckoutLineDiscount.objects.filter(line=kit_line).exists()
    assert calculate_base_line_unit_price(info) == Money(SALE_UNIT, USD)


def test_the_predicate_reads_the_column_and_costs_nothing(django_assert_num_queries):
    """One attribute test on a line already in memory, and no dealer reason."""

    class _Line:
        def __init__(self, reason):
            self.price_override_reason = reason

    with django_assert_num_queries(0):
        assert is_sale_priced_line(_Line(KIT_REASON))
        assert is_sale_priced_line(_Line(COMPOSE_REASON))
        assert not is_sale_priced_line(_Line("wsm.dealer"))
        assert not is_sale_priced_line(_Line(None))
