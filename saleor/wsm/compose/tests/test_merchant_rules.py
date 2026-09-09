# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The rules that stop the admin saving a product a shopper cannot buy.

Every case here comes from the merchant walk of 2026-09-08, where a staff user
saved a price of -9999.00 on a $649 product, was told it had saved, and left a
buy button that answered 422. These are model tests on purpose: the refusal
belongs to the row, so the admin, the inline and any writer that validates get
the same answer from one place.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from saleor.wsm.compose import pricing
from saleor.wsm.compose.forms import OptionValueInlineFormSet
from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    Fee,
    OptionSet,
    OptionValue,
)
from saleor.wsm.dealer.models import DealerGroup

LISTED_PRICE = Decimal("649.00")


@pytest.fixture
def listed_product(variant, channel_USD):
    """A $649 product, the one the merchant walk broke."""
    # EVERY listing on the product: Saleor's fixture hangs a second variant off
    # it at 10.00, and the floor is taken from the cheapest one, so leaving that
    # behind would test a $10 product and call it $649.
    from saleor.product.models import ProductVariantChannelListing

    ProductVariantChannelListing.objects.filter(
        variant__product=variant.product
    ).update(price_amount=LISTED_PRICE, discounted_price_amount=LISTED_PRICE)
    return variant.product


@pytest.fixture
def finish(listed_product):
    return OptionSet.objects.create(
        product=listed_product,
        name="Choose a finish",
        prompt_type=pricing.CHOICE_ONE,
        required=True,
    )


# --- 1. a price that breaks add-to-cart ------------------------------------


def test_a_credit_larger_than_the_product_price_is_refused(finish):
    """The walk's exact row: -9999.00 on a $649 product."""
    value = OptionValue(option_set=finish, name="Raw", price_delta=Decimal("-9999.00"))

    with pytest.raises(ValidationError) as caught:
        value.full_clean()

    assert "price_delta" in caught.value.message_dict
    message = caught.value.message_dict["price_delta"][0]
    # In currency units, naming both ends, so the merchant knows what to change.
    assert "-9,350.00" in message
    assert "649.00" in message
    assert "cents" not in message


def test_credits_that_only_go_negative_together_are_refused(finish):
    """Each row is legal against the product; the pair is not."""
    finish.prompt_type = pricing.CHOICE_MANY
    finish.save(update_fields=["prompt_type"])
    first = OptionValue.objects.create(
        option_set=finish, name="Omit pump", price_delta=Decimal("-400.00")
    )
    assert first.pk

    second = OptionValue(
        option_set=finish, name="Omit filter", price_delta=Decimal("-400.00")
    )
    with pytest.raises(ValidationError) as caught:
        second.full_clean()

    assert "-151.00" in caught.value.message_dict["price_delta"][0]


def test_a_credit_the_product_can_carry_saves(finish):
    """The rule has to let the ordinary case through, or it is just a wall."""
    value = OptionValue(
        option_set=finish, name="Omit pump", price_delta=Decimal("-400.00")
    )
    value.full_clean()
    value.save()

    assert OptionValue.objects.get(pk=value.pk).price_delta == Decimal("-400.00")


def test_editing_a_credit_back_down_is_not_blocked_by_its_own_stored_row(finish):
    """A row is compared against the catalog WITHOUT its stored self."""
    value = OptionValue.objects.create(
        option_set=finish, name="Omit pump", price_delta=Decimal("-600.00")
    )

    value.price_delta = Decimal("-620.00")
    value.full_clean()  # 649 - 620 = 29.00, still above zero


def test_a_product_with_no_listed_price_is_not_second_guessed(finish):
    """Half a catalog is not a reason to refuse the other half."""
    for variant in finish.product.variants.all():
        variant.channel_listings.all().delete()

    OptionValue(
        option_set=finish, name="Raw", price_delta=Decimal("-9999.00")
    ).full_clean()


def test_making_a_set_required_is_refused_when_its_cheapest_answer_goes_negative(
    listed_product,
):
    """The floor moves from the set side too, not only from a price."""
    option_set = OptionSet.objects.create(
        product=listed_product,
        name="Trade in your old unit",
        prompt_type=pricing.CHOICE_ONE,
        required=False,
    )
    OptionValue.objects.create(
        option_set=option_set, name="Trade in", price_delta=Decimal("-700.00")
    )
    OptionValue.objects.create(
        option_set=option_set, name="No thanks", price_delta=Decimal(0)
    )

    option_set.required = True
    with pytest.raises(ValidationError) as caught:
        option_set.full_clean()

    assert "-51.00" in str(caught.value)


def test_three_new_credits_in_one_submit_are_refused_together(finish):
    """A row at a time is blind to its siblings in the same POST."""
    finish.prompt_type = pricing.CHOICE_MANY
    finish.save(update_fields=["prompt_type"])

    formset = _value_formset(
        finish,
        [
            {"name": "Omit A", "price_delta": "-300.00", "sort_order": "1"},
            {"name": "Omit B", "price_delta": "-300.00", "sort_order": "2"},
            {"name": "Omit C", "price_delta": "-300.00", "sort_order": "3"},
        ],
    )

    assert not formset.is_valid()
    assert "-251.00" in str(formset.non_form_errors())


# --- 2. a duplicate SKU code inside one question ---------------------------


def test_a_second_choice_with_the_same_sku_code_is_refused(finish):
    OptionValue.objects.create(
        option_set=finish,
        name="Dual pumps",
        sku_fragment="49614",
        price_delta=Decimal(1000),
    )

    clash = OptionValue(
        option_set=finish,
        name="Triple pumps",
        sku_fragment="49614",
        price_delta=Decimal(1500),
    )
    with pytest.raises(ValidationError) as caught:
        clash.full_clean()

    message = caught.value.message_dict["sku_fragment"][0]
    assert "Dual pumps" in message
    assert "49614" in message


def test_blank_sku_codes_never_collide(finish):
    """Most values carry no fragment; twenty blanks in Fuel Lab's own data."""
    OptionValue.objects.create(option_set=finish, name="Black", price_delta=Decimal(0))
    OptionValue(option_set=finish, name="Silver", price_delta=Decimal(0)).full_clean()


def test_the_same_code_on_two_different_questions_is_fine(listed_product, finish):
    other = OptionSet.objects.create(product=listed_product, name="Choose a length")
    OptionValue.objects.create(
        option_set=finish, name="Black", sku_fragment="BLK", price_delta=Decimal(0)
    )

    OptionValue(
        option_set=other, name="Black", sku_fragment="BLK", price_delta=Decimal(0)
    ).full_clean()


def test_two_new_choices_sharing_a_code_in_one_submit_are_refused(finish):
    formset = _value_formset(
        finish,
        [
            {"name": "Dual pumps", "sku_fragment": "49614", "price_delta": "1000.00"},
            {"name": "Triple pumps", "sku_fragment": "49614", "price_delta": "1500.00"},
        ],
    )

    assert not formset.is_valid()
    assert "49614" in str(formset.errors)


def test_the_5_0_import_can_still_write_the_collisions_fuel_lab_already_has(finish):
    """No UNIQUE index, on purpose: eight colliding pairs are live in fub today.

    A database constraint would refuse the import that produced them, which is a
    worse outcome than the ambiguous SKU it would prevent. The rule guards the
    merchant screens; the source rows are a reported data fix.
    """
    for name in ("Dual pumps", "Triple pumps"):
        OptionValue.objects.create(
            option_set=finish,
            name=name,
            sku_fragment="49614",
            price_delta=Decimal(1000),
        )

    assert OptionValue.objects.filter(sku_fragment="49614").count() == 2


# --- 3. a dealer group that does not exist ---------------------------------


def test_a_tier_group_naming_no_dealer_group_is_refused(finish):
    value = OptionValue.objects.create(
        option_set=finish, name="Black", price_delta=Decimal(0)
    )
    DealerGroup.objects.create(code="dealer-1")

    row = DealerTierOptionPrice(
        option_value=value, tier_group="gold", price_delta=Decimal(0)
    )
    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    message = caught.value.message_dict["tier_group"][0]
    assert "gold" in message
    assert "dealer-1" in message


def test_a_tier_group_that_exists_saves(finish):
    value = OptionValue.objects.create(
        option_set=finish, name="Black", price_delta=Decimal(0)
    )
    DealerGroup.objects.create(code="dealer-1")

    row = DealerTierOptionPrice(
        option_value=value, tier_group="dealer-1", price_delta=Decimal(0)
    )
    row.full_clean()
    row.save()

    assert row.pk


# --- 4. a charge a merchant cannot mean ------------------------------------


def test_a_negative_charge_is_refused(listed_product):
    fee = Fee(
        product=listed_product,
        label="Discount",
        basis=pricing.FIXED,
        amount=Decimal("-20.00"),
    )
    with pytest.raises(ValidationError) as caught:
        fee.full_clean()

    assert "cannot be negative" in caught.value.message_dict["amount"][0]


def test_a_percentage_charge_above_100_is_refused(listed_product):
    fee = Fee(
        product=listed_product,
        label="Handling",
        basis=pricing.PERCENT,
        amount=Decimal("825.00"),
    )
    with pytest.raises(ValidationError) as caught:
        fee.full_clean()

    assert "more than 100%" in caught.value.message_dict["amount"][0]


def test_a_percentage_charge_of_8_25_saves(listed_product):
    """8.25 means 8.25%, which is what the help text now says on the field."""
    fee = Fee(
        product=listed_product,
        label="Handling",
        basis=pricing.PERCENT,
        amount=Decimal("8.25"),
    )
    fee.full_clean()
    fee.save()

    assert fee.to_pricing().amount == 825


def test_a_flat_charge_above_100_is_fine(listed_product):
    """A $125 crate is not a decimal-point slip."""
    Fee(
        product=listed_product,
        label="Freight crating",
        basis=pricing.FIXED,
        amount=Decimal("125.00"),
    ).full_clean()


# --- 5. the message a shopper reads ----------------------------------------


def test_the_refusal_a_shopper_reads_is_money_not_cents():
    with pytest.raises(pricing.NegativeTotalError) as caught:
        pricing.price_configured(
            10000,
            [
                pricing.OptionSet(
                    id=1,
                    required=True,
                    values=(pricing.Value(id=1, price_delta=-935000),),
                )
            ],
            [pricing.Selection(set_id=1, value_ids=(1,))],
        )

    message = str(caught.value)
    assert "-9,250.00" in message
    assert "cents" not in message
    assert caught.value.total_cents == -925000


# --- the formset the admin builds, without a browser -----------------------


def _value_formset(option_set, rows):
    """The inline formset OptionSetAdmin renders, fed a POST by hand."""
    from django.forms.models import inlineformset_factory

    from saleor.wsm.compose.forms import OptionValueInlineForm

    factory = inlineformset_factory(
        OptionSet,
        OptionValue,
        form=OptionValueInlineForm,
        formset=OptionValueInlineFormSet,
        fields=("name", "sku_fragment", "price_delta", "image_url", "sort_order"),
        extra=len(rows),
    )
    prefix = "values"
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for field in ("name", "sku_fragment", "price_delta", "image_url", "sort_order"):
            data[f"{prefix}-{index}-{field}"] = row.get(
                field, "0" if field == "sort_order" else ""
            )
    return factory(data, instance=option_set, prefix=prefix)
