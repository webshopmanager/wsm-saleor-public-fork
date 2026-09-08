# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The stored rows produce the fixture numbers.

test_pricing.py proves the arithmetic on plain dataclasses. This proves the other
half: real rows in our own tables, converted by `to_pricing()`, reach the same
answer. Without it a Decimal-to-cents slip would pass every test above.
"""

from decimal import Decimal

import pytest

from saleor.product import ProductTypeKind
from saleor.product.models import Product, ProductType
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


def test_stored_tier_row_on_a_credit_is_verbatim(credit_sets):
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
    assert result.unit_cents == 399899 - 30000
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
