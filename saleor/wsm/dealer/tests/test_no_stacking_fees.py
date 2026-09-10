# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches").
"""A fee line is never discountable, required or optional, dealer cart or retail.

The defect these close (live probe P10 on the bake-off box, 2026-09-08): a cart
holding one dealer-priced configured item and the required crate charge that
comes with it, plus a 100.00 fixed ENTIRE_ORDER voucher. MP2 kept the discount
off the dealer line, so Saleor spread the whole 100.00 over the only other line
in the cart and CRATE-01 billed at 49.00 instead of 149.00. A voucher over the
charge would have zeroed it. Retail carts were open the same way: every fee line
in the fleet was discountable.

5.0 is the default this restores: a coupon came off the merchandise subtotal and
never off a product fee, because a fee is a pass-through charge (crating, core,
environmental), not margin the merchant is free to give away.

`required` is deliberately not consulted. It lives on the `Fee` row and is not
snapshotted onto the line: the private `compose.fee` stamp carries the label and
`apply_to` only. It does not need to be, because the answer is the same either
way, and reading a flag that is not there is how the exclusion would get it
wrong.
"""

import json
from decimal import Decimal

import graphene
import pytest
from prices import Money

from ....checkout.base_calculations import (
    checkout_total,
    get_line_total_price_with_propagated_checkout_discount,
)
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....checkout.utils import add_voucher_to_checkout
from ....discount import (
    DiscountType,
    DiscountValueType,
    RewardValueType,
    VoucherType,
)
from ....discount.models import (
    CheckoutLineDiscount,
    Promotion,
    Voucher,
    VoucherChannelListing,
    VoucherCode,
)
from ....discount.utils.checkout import (
    create_checkout_line_discount_objects_for_catalogue_promotions,
)
from ....plugins.manager import get_plugins_manager
from ....product.models import (
    Product,
    ProductVariant,
    ProductVariantChannelListing,
)
from ..no_stacking import (
    FEE_METADATA_KEY,
    LINE_METADATA_KEY,
    PRICE_OVERRIDE_REASON,
    line_of_info,
    split_discountable,
)

pytestmark = pytest.mark.django_db

USD = "USD"
MERCH_UNIT = Decimal("500.00")
FEE_UNIT = Decimal("149.00")
COMPOSE_REASON = "wsm.compose"


def test_the_fee_marker_is_the_one_compose_writes():
    """The literal is repeated to break an import cycle, so it is pinned here.

    `saleor.wsm.compose.views` imports this module for the dealer key, so this
    module cannot import it back for the fee key. If the two ever drift, every
    fee line in the fleet becomes discountable again, silently.
    """
    from ...compose.lines import META_FEE

    assert FEE_METADATA_KEY == META_FEE


@pytest.fixture
def merch_variant(product, channel_USD):
    """The configured item. Priced from its listing, like any merchandise."""
    variant = ProductVariant.objects.create(
        product=product, sku="MERCH-01", track_inventory=False
    )
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel_USD,
        price_amount=MERCH_UNIT,
        discounted_price_amount=MERCH_UNIT,
        currency=channel_USD.currency_code,
    )
    return variant


@pytest.fixture
def fee_variant(product_type, category, channel_USD):
    """What `Fee.ensure_variant` mints: its own product, listed at ZERO.

    The charge is not on the listing, it is the `price_override` the add endpoint
    writes onto the line from the pricing engine, which is why a fee line looks
    like a free product to anything that reads the catalog.
    """
    fee_product = Product.objects.create(
        name="Crating charge",
        slug="wsm-fee-1",
        product_type=product_type,
        category=category,
    )
    variant = ProductVariant.objects.create(
        product=fee_product, sku="CRATE-01", track_inventory=False
    )
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel_USD,
        price_amount=Decimal(0),
        discounted_price_amount=Decimal(0),
        currency=channel_USD.currency_code,
    )
    return variant


@pytest.fixture
def hundred_off(channel_USD):
    """100.00 off the whole order, the shape probe P10 used."""
    voucher = Voucher.objects.create(
        name="100 off", type=VoucherType.ENTIRE_ORDER
    )
    VoucherCode.objects.create(voucher=voucher, code="HUNDRED")
    VoucherChannelListing.objects.create(
        voucher=voucher, channel=channel_USD, discount=Money(Decimal(100), USD)
    )
    return voucher


@pytest.fixture
def ten_percent_off(channel_USD):
    voucher = Voucher.objects.create(
        name="10 percent off",
        type=VoucherType.ENTIRE_ORDER,
        discount_value_type=DiscountValueType.PERCENTAGE,
    )
    VoucherCode.objects.create(voucher=voucher, code="TENPERCENT")
    VoucherChannelListing.objects.create(
        voucher=voucher, channel=channel_USD, discount_value=Decimal(10)
    )
    return voucher


@pytest.fixture
def cart(checkout, merch_variant, fee_variant, channel_USD, address):
    """A configured line plus the charge hanging off it, dealer-priced or not.

    The stamps are the ones the key-gated add endpoint writes into PRIVATE
    metadata: `wsm.dealer` when the line took a tier, `compose.fee` on the
    charge.
    """

    def build(dealer: bool, voucher=None):
        merch_line = CheckoutLine.objects.create(
            checkout=checkout,
            variant=merch_variant,
            quantity=1,
            currency=channel_USD.currency_code,
            price_override=Decimal("400.00") if dealer else None,
            price_override_reason=PRICE_OVERRIDE_REASON if dealer else None,
            undiscounted_unit_price_amount=(
                Decimal("400.00") if dealer else MERCH_UNIT
            ),
            private_metadata=(
                {LINE_METADATA_KEY: json.dumps({"group": "dealer-1"})}
                if dealer
                else {}
            ),
        )
        fee_line = CheckoutLine.objects.create(
            checkout=checkout,
            variant=fee_variant,
            quantity=1,
            currency=channel_USD.currency_code,
            price_override=FEE_UNIT,
            price_override_reason=COMPOSE_REASON,
            undiscounted_unit_price_amount=FEE_UNIT,
            private_metadata={
                FEE_METADATA_KEY: json.dumps(
                    {"label": "Crating", "apply_to": "per_unit"}
                )
            },
        )
        checkout.billing_address = address
        checkout.shipping_address = address
        checkout.save()

        manager = get_plugins_manager(allow_replica=False)
        lines_info, _ = fetch_checkout_lines(checkout)
        checkout_info = fetch_checkout_info(checkout, lines_info, manager)
        if voucher is not None:
            add_voucher_to_checkout(
                manager,
                checkout_info,
                lines_info,
                voucher,
                voucher.codes.first(),
            )
        return checkout_info, lines_info, merch_line, fee_line

    return build


def propagated(checkout_info, lines_info):
    return {
        info.line.pk: get_line_total_price_with_propagated_checkout_discount(
            checkout_info, lines_info, info
        )
        for info in lines_info
    }


def test_a_voucher_finds_nothing_to_discount_on_a_dealer_line_and_its_charge(
    cart, hundred_off
):
    """Probe P10's exact shape. Before this the charge billed at 49.00.

    Nothing in the cart is discountable: the dealer line is the price the
    merchant already negotiated, and the charge is money owed to a crate. Saleor
    is handed an empty line set, sizes the discount on a subtotal of zero and
    ACCEPTS the code at 0.00 rather than refusing it, because the code itself is
    valid and its minimum spend, had one been set, is what would refuse it.
    For the bake-off that is the outcome we want on the money and a thin one for
    the shopper, who sees the code accepted and the total unchanged; noted in the
    design doc as the one place the shopper is under-informed.
    """
    checkout_info, lines_info, merch_line, fee_line = cart(
        dealer=True, voucher=hundred_off
    )

    assert checkout_info.checkout.discount == Money(Decimal("0.00"), USD)

    by_line = propagated(checkout_info, lines_info)
    assert by_line[merch_line.pk] == Money(Decimal("400.00"), USD)
    assert by_line[fee_line.pk] == Money(FEE_UNIT, USD)
    assert checkout_total(checkout_info, lines_info) == Money(
        Decimal("549.00"), USD
    )


def test_a_fixed_voucher_lands_entirely_on_the_retail_line_not_the_charge(
    cart, hundred_off
):
    """Retail merchandise is discountable; the charge beside it is not.

    Stock Saleor spreads 100.00 over 649.00 of lines, so the charge would take
    22.96 of it. The whole 100.00 belongs on the merchandise.
    """
    checkout_info, lines_info, merch_line, fee_line = cart(
        dealer=False, voucher=hundred_off
    )

    assert checkout_info.checkout.discount == Money(Decimal("100.00"), USD)

    by_line = propagated(checkout_info, lines_info)
    assert by_line[merch_line.pk] == Money(Decimal("400.00"), USD)
    assert by_line[fee_line.pk] == Money(FEE_UNIT, USD)
    assert checkout_total(checkout_info, lines_info) == Money(
        Decimal("549.00"), USD
    )


def test_a_percentage_voucher_is_sized_on_the_merchandise_alone(
    cart, ten_percent_off
):
    """Ten percent of 500.00, not of 649.00: the charge is not in the base.

    This is the AMOUNT half. Sizing on the whole cart and then spreading only
    over the merchandise would be worse than stock, because the shopper would
    get 64.90 off a 500.00 item for having a crate charge in the cart.
    """
    checkout_info, lines_info, merch_line, fee_line = cart(
        dealer=False, voucher=ten_percent_off
    )

    assert checkout_info.checkout.discount == Money(Decimal("50.00"), USD)

    by_line = propagated(checkout_info, lines_info)
    assert by_line[merch_line.pk] == Money(Decimal("450.00"), USD)
    assert by_line[fee_line.pk] == Money(FEE_UNIT, USD)


def test_a_catalogue_promotion_over_the_whole_catalog_skips_the_fee_line(
    cart, channel_USD, merch_variant, fee_variant
):
    """MP1's half, through the real entry point, with real discount rows.

    A fee variant is an ordinary product variant with an ordinary channel
    listing, so a merchant promotion written against a category or the whole
    catalog sweeps it up. Here the rule names both products, and only the
    merchandise line may end up with a discount row. The stale row planted on
    the fee line is the second half of the rule: a promotion that landed before
    the line was a fee line has to come back off.
    """
    checkout_info, lines_info, merch_line, fee_line = cart(dealer=False)

    promotion = Promotion.objects.create(name="Everything must go")
    rule = promotion.rules.create(
        catalogue_predicate={
            "productPredicate": {
                "ids": [
                    graphene.Node.to_global_id("Product", merch_variant.product_id),
                    graphene.Node.to_global_id("Product", fee_variant.product_id),
                ]
            }
        },
        reward_value_type=RewardValueType.FIXED,
        reward_value=Decimal(5),
    )
    rule.channels.add(channel_USD)
    for variant in (merch_variant, fee_variant):
        listing = variant.channel_listings.get(channel=channel_USD)
        # A fee variant is minted with a ZERO listing, and a zero listing can
        # never be marked discounted, so the mint shape hides this by accident.
        # That is not a rule: the fee product is an ordinary catalog row and a
        # merchant price on it, or an import that sets one, puts it back in
        # reach. Priced here the way a merchant would, so the test measures the
        # guard and not the accident. The money that would then move is the
        # CHARGE: once the listing says the line is on promotion, the stock code
        # takes the reward off `line.price_override`
        # (saleor/discount/utils/promotion.py lines 202-216), which on a fee line
        # is the 149.00 the crate costs.
        listing.price_amount = FEE_UNIT if variant is fee_variant else MERCH_UNIT
        listing.discounted_price_amount = listing.price_amount - Decimal(5)
        listing.save(update_fields=["price_amount", "discounted_price_amount"])
        listing.variantlistingpromotionrule.create(
            promotion_rule=rule,
            discount_amount=Decimal(5),
            currency=channel_USD.currency_code,
        )
    # A row that landed before the line carried the stamp.
    CheckoutLineDiscount.objects.create(
        line=fee_line,
        type=DiscountType.PROMOTION,
        value_type=DiscountValueType.FIXED,
        value=Decimal(5),
        amount_value=Decimal(5),
        currency=channel_USD.currency_code,
        promotion_rule=rule,
    )

    lines_info, _ = fetch_checkout_lines(checkout_info.checkout)
    create_checkout_line_discount_objects_for_catalogue_promotions(lines_info)

    assert not CheckoutLineDiscount.objects.filter(line=fee_line).exists()
    assert CheckoutLineDiscount.objects.filter(line=merch_line).count() == 1


def test_a_cart_of_retail_lines_and_charges_never_asks_the_dealer_toggle(
    django_assert_num_queries, fee_variant
):
    """The cost claim in `split_discountable`, held to.

    The toggle is one indexed single-row read, and it is only ever worth making
    on a cart that has a dealer line in it. A fee is decided by a dict-key test
    on a line already in memory.
    """

    class _Line:
        def __init__(self, stamps):
            self.private_metadata = stamps

    lines = [_Line({}), _Line({FEE_METADATA_KEY: "{}"}), _Line({})]

    with django_assert_num_queries(0):
        eligible, excluded = split_discountable(lines)

    assert len(eligible) == 2
    assert len(excluded) == 1

    # And the LineInfo shape the checkout paths carry, through the same function.
    class _Info:
        def __init__(self, line):
            self.line = line

    eligible, excluded = split_discountable(
        [_Info(line) for line in lines], line_of_info
    )
    assert [info.line for info in excluded] == [lines[1]]
