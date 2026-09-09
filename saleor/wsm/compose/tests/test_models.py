# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The stored rows produce the fixture numbers.

test_pricing.py proves the arithmetic on plain dataclasses. This proves the other
half: real rows in our own tables, converted by `to_pricing()`, reach the same
answer. Without it a Decimal-to-cents slip would pass every test above.
"""

from decimal import Decimal

import pytest

from django.core.exceptions import ValidationError

from saleor.product import ProductTypeKind
from saleor.product.models import (
    Product,
    ProductType,
    ProductVariant,
    ProductVariantChannelListing,
)
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    Fee,
    OptionSet,
    OptionValue,
    to_cents,
)
from saleor.wsm.compose.pricing import Selection, price_configured

CREDITS = [
    ("Cam bearings", "NCB", Decimal("-29.99")),
    ("Plug kit", "NPK", Decimal("-30.00")),
    ("Gasket set", "NGS", Decimal("-445.00")),
]


@pytest.fixture
def dd_product(db):
    product_type = ProductType.objects.create(
        name="Ultimate kit", slug="ultimate-kit", kind=ProductTypeKind.NORMAL
    )
    return Product.objects.create(
        name="Stage 2 Ultimate kit", slug="l600084", product_type=product_type
    )


@pytest.fixture
def credit_sets(dd_product):
    sets = []
    for order, (name, fragment, delta) in enumerate(CREDITS, start=1):
        option_set = OptionSet.objects.create(
            product=dd_product, name=name, sort_order=order
        )
        OptionValue.objects.create(
            option_set=option_set, name="Delete", sku_fragment=fragment, price_delta=delta
        )
        sets.append(option_set)
    return sets


@pytest.mark.parametrize(
    ("base", "expected_cents"), [(Decimal("3998.99"), 349400), (Decimal("6399.00"), 589401)]
)
def test_stored_rows_reach_the_fixture_number(credit_sets, base, expected_cents):
    result = price_configured(
        to_cents(base),
        [s.to_pricing() for s in credit_sets],
        [Selection(set_id=s.pk, value_ids=(s.values.get().pk,)) for s in credit_sets],
        base_sku="L600084",
    )
    assert result.unit_cents == expected_cents
    assert result.composite_sku == "L600084-NCB-NPK-NGS"


def test_a_stored_tier_row_worse_than_retail_leaves_the_dealer_at_retail(credit_sets):
    """The stored half of verdict 7: better of, off real rows."""
    gasket = credit_sets[-1]
    value = gasket.values.get()
    DealerTierOptionPrice.objects.create(
        option_value=value, tier_group="dealer-1", price_delta=Decimal("-300.00")
    )
    result = price_configured(
        to_cents(Decimal("3998.99")),
        [gasket.to_pricing()],
        [Selection(set_id=gasket.pk, value_ids=(value.pk,))],
        "dealer-1",
    )
    assert result.unit_cents == 355399
    assert result.snapshot["tier_applied"] is False


def test_a_stored_tier_row_deeper_than_retail_is_the_one_charged(credit_sets):
    gasket = credit_sets[-1]
    value = gasket.values.get()
    DealerTierOptionPrice.objects.create(
        option_value=value, tier_group="dealer-1", price_delta=Decimal("-500.00")
    )
    result = price_configured(
        to_cents(Decimal("3998.99")),
        [gasket.to_pricing()],
        [Selection(set_id=gasket.pk, value_ids=(value.pk,))],
        "dealer-1",
    )
    assert result.unit_cents == 349899
    assert result.snapshot["tier_applied"] is True


def test_stored_percent_fee_reads_amount_as_hundredths_of_a_percent(dd_product):
    fee = Fee.objects.create(
        product=dd_product,
        label="Handling",
        basis=pricing.PERCENT,
        amount=Decimal("8.25"),
        apply_to=pricing.PER_UNIT,
    )
    result = price_configured(10010, [], [], fees=[fee.to_pricing()], quantity=3)
    assert result.fee_total_cents == 2478


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("-29.99"), -2999),
        (Decimal("0"), 0),
        (Decimal("3998.99"), 399899),
        (Decimal("8.25"), 825),
    ],
)
def test_to_cents_is_exact(amount, expected):
    assert to_cents(amount) == expected


@pytest.fixture
def crating_fee(dd_product):
    return Fee.objects.create(
        product=dd_product,
        label="Freight crating",
        sku="CRATE-01",
        basis="fixed",
        amount=Decimal("125.00"),
        apply_to="unit",
        required=True,
    )


def test_a_second_shopper_racing_the_same_fee_gets_the_row_the_first_made(
    crating_fee, channel_USD
):
    """The first add of a fee is a shopper request that writes catalog rows."""
    from saleor.wsm.compose import models as compose_models

    first = compose_models._ensure_fee_variant(crating_fee, channel_USD)

    # The loser of the race: same fee row, its own process, nothing memoised
    # and no variant on the copy it read a moment before the winner saved.
    compose_models._ENSURED_FEE_VARIANTS.discard((crating_fee.pk, channel_USD.pk))
    loser = Fee.objects.get(pk=crating_fee.pk)
    loser.variant = None

    second = compose_models._ensure_fee_variant(loser, channel_USD)

    assert second.pk == first.pk
    assert Product.objects.filter(slug=f"wsm-fee-{crating_fee.pk}").count() == 1


def test_the_fee_variant_sku_is_ours_and_the_merchant_sku_still_ships(
    crating_fee, channel_USD
):
    """A fee's hidden variant never claims the merchant's SKU."""
    from saleor.wsm.compose import models as compose_models

    variant = compose_models._ensure_fee_variant(crating_fee, channel_USD)

    assert variant.sku == f"wsm-fee-{crating_fee.pk}"
    # CRATE-01 is what the ERP reads, and it rides the priced snapshot.
    assert crating_fee.to_pricing().sku == "CRATE-01"


# --- verdict 7: the two rules a tier row is saved under ----------------------


@pytest.fixture
def listed_product(dd_product, channel_USD):
    """A product with a price, which is what makes a configured floor a number."""
    variant = ProductVariant.objects.create(product=dd_product, sku="L600084")
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel_USD,
        price_amount=Decimal("400.00"),
        discounted_price_amount=Decimal("400.00"),
        currency=channel_USD.currency_code,
    )
    return dd_product


@pytest.fixture
def dealer_1(db):
    from saleor.wsm.dealer.models import DealerGroup

    return DealerGroup.objects.create(code="dealer-1", name="Dealer 1")


def a_value(product, delta):
    option_set = OptionSet.objects.create(product=product, name="Finish")
    return OptionValue.objects.create(
        option_set=option_set, name="Titanium", sku_fragment="TI", price_delta=delta
    )


def test_a_tier_row_above_retail_is_refused_where_the_merchant_can_fix_it(
    listed_product, dealer_1
):
    """It used to save, quote retail on the page, and refuse the add to cart.

    The ceiling lived in `pricing.delta_for`, two layers from the screen, and it
    raised at add-to-cart time with a message written for a developer. Same test,
    said as a field error on the row that breaks it.
    """
    value = a_value(listed_product, Decimal("100.00"))
    row = DealerTierOptionPrice(
        option_value=value, tier_group="dealer-1", price_delta=Decimal("150.00")
    )

    with pytest.raises(ValidationError) as refused:
        row.full_clean()

    assert "price_delta" in refused.value.message_dict
    assert "100.00" in str(refused.value)


def test_a_tier_credit_that_takes_this_group_to_nothing_is_refused(
    listed_product, dealer_1
):
    """The retail floor cannot see this one: retail stops at 300.00, safely up.

    The dealer pays the better of its own delta and retail on every choice, so a
    -450.00 dealer credit on a 400.00 product is a configuration that comes to
    less than nothing FOR THAT GROUP, and every dealer add-to-cart on it would
    have refused at checkout instead.
    """
    value = a_value(listed_product, Decimal("-100.00"))
    row = DealerTierOptionPrice(
        option_value=value, tier_group="dealer-1", price_delta=Decimal("-450.00")
    )

    with pytest.raises(ValidationError) as refused:
        row.full_clean()

    assert "dealer-1" in str(refused.value)
    assert "-50.00" in str(refused.value)


def test_a_tier_credit_the_product_can_carry_still_saves(listed_product, dealer_1):
    """The rule is a floor, not a ban on dealer credits."""
    value = a_value(listed_product, Decimal("-100.00"))

    DealerTierOptionPrice(
        option_value=value, tier_group="dealer-1", price_delta=Decimal("-150.00")
    ).full_clean()


FEE_PRICING_QUERY = """
    query FeeProduct($slug: String!, $channel: String!) {
      product(slug: $slug, channel: $channel) {
        productType { slug hasVariants isShippingRequired }
        variants { sku pricing { price { gross { amount } } } }
      }
    }
"""


def test_a_minted_fee_product_answers_variant_pricing_anonymously(
    crating_fee, channel_USD, api_client
):
    """A fee product is a public URL, so its public fields have to resolve.

    Stock `get_variant_availability` guards a NULL `price` and then dereferences
    `discounted_price` unguarded, so a variant listing carrying only the first
    of the two amounts turns `variants { pricing }` into a 500 for anyone, with
    no login, on every fee a merchant has ever sold.
    """
    from saleor.graphql.tests.utils import get_graphql_content
    from saleor.wsm.compose import models as compose_models

    compose_models._ensure_fee_variant(crating_fee, channel_USD)

    response = api_client.post_graphql(
        FEE_PRICING_QUERY,
        {"slug": f"wsm-fee-{crating_fee.pk}", "channel": channel_USD.slug},
    )
    product = get_graphql_content(response)["data"]["product"]

    amounts = [v["pricing"]["price"]["gross"]["amount"] for v in product["variants"]]
    assert amounts == [0.0]


def test_a_fee_variant_listing_minted_before_the_fix_is_repaired_in_place(
    crating_fee, channel_USD
):
    """The rows already in the two bake-off databases repair themselves.

    A one-off management command would be a second way to do the same write.
    The minting path already reads this listing on every configured add, so the
    repair is a branch on a row it holds, costing one UPDATE once per bad row.
    """
    from saleor.product.models import ProductVariantChannelListing
    from saleor.wsm.compose import models as compose_models

    variant = compose_models._ensure_fee_variant(crating_fee, channel_USD)
    listing = ProductVariantChannelListing.objects.get(
        variant=variant, channel=channel_USD
    )
    # The shape the fee minter wrote before this fix, and the shape the four
    # live rows are in today.
    ProductVariantChannelListing.objects.filter(pk=listing.pk).update(
        discounted_price_amount=None
    )
    compose_models._ENSURED_FEE_VARIANTS.discard((crating_fee.pk, channel_USD.pk))

    compose_models._ensure_fee_variant(Fee.objects.get(pk=crating_fee.pk), channel_USD)

    listing.refresh_from_db()
    assert listing.discounted_price_amount == Decimal("0")


def test_fee_products_are_excludable_by_product_type(crating_fee, channel_USD):
    """`wsm-fee` is the key the storefront, sitemap and feed exclude on.

    Named here so a rename breaks a test in this repo rather than a page in
    another one. Not shipping required: a Fee carries no freight marker of its
    own (`freight_class` lives on KitConfig, on the kit, not on the charge), so
    the charge itself never asks the shipping engine for a rate.
    """
    from saleor.product.models import ProductChannelListing
    from saleor.wsm.compose import models as compose_models

    variant = compose_models._ensure_fee_variant(crating_fee, channel_USD)

    product_type = variant.product.product_type
    assert product_type.slug == "wsm-fee"
    assert product_type.has_variants is False
    assert product_type.is_shipping_required is False
    listing = ProductChannelListing.objects.get(
        product=variant.product, channel=channel_USD
    )
    assert listing.visible_in_listings is False
