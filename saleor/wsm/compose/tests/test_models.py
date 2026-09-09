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
    CONFIGURABLE_METAFIELD,
    CONFIGURABLE_VALUE,
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


@pytest.mark.django_db
def test_a_question_added_by_hand_turns_the_configurator_on(product):
    """Only the 5.0 importer ever wrote the marker the storefront gates on.

    A merchant who built a set in /admin/ got a working set, a working price and
    a PDP that asked nothing: the one screen the feature exists for.
    """
    assert CONFIGURABLE_METAFIELD not in product.metadata

    OptionSet.objects.create(product=product, name="Color", label="Colour")

    product.refresh_from_db()
    assert product.metadata[CONFIGURABLE_METAFIELD] == CONFIGURABLE_VALUE


@pytest.mark.django_db
def test_a_charge_alone_makes_a_product_configurable(product):
    """A declinable crating charge is something the PDP has to ask about."""
    Fee.objects.create(
        product=product, label="Crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    product.refresh_from_db()
    assert product.metadata[CONFIGURABLE_METAFIELD] == CONFIGURABLE_VALUE


@pytest.mark.django_db
def test_removing_the_last_one_turns_it_off_again(product):
    """The mirror defect: a configurator on a product with nothing to configure."""
    option_set = OptionSet.objects.create(product=product, name="Color")
    fee = Fee.objects.create(
        product=product, label="Crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    option_set.delete()
    product.refresh_from_db()
    assert product.metadata[CONFIGURABLE_METAFIELD] == CONFIGURABLE_VALUE

    fee.delete()
    product.refresh_from_db()
    assert CONFIGURABLE_METAFIELD not in product.metadata


@pytest.mark.django_db
def test_a_bulk_delete_updates_the_marker_too(product):
    """The admin's "delete selected" never calls `Model.delete`."""
    OptionSet.objects.create(product=product, name="Color")

    OptionSet.objects.filter(product=product).delete()

    product.refresh_from_db()
    assert CONFIGURABLE_METAFIELD not in product.metadata


@pytest.mark.django_db
def test_the_importer_and_the_admin_write_the_same_marker():
    """Two copies of the string, one storefront contract."""
    from saleor.wsm.compose.management.commands.import_option_sets_50 import (
        CONFIGURABLE_METAFIELD as IMPORTED_FIELD,
    )
    from saleor.wsm.compose.management.commands.import_option_sets_50 import (
        CONFIGURABLE_VALUE as IMPORTED_VALUE,
    )

    assert (IMPORTED_FIELD, IMPORTED_VALUE) == (
        CONFIGURABLE_METAFIELD,
        CONFIGURABLE_VALUE,
    )
