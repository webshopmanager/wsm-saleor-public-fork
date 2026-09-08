# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The pricing contract, with no database in the way.

The oracle is the fixture table in REQUIREMENTS-configured-pricing-2026-09-08
section 1.2: Chris Campion's two real Dynamic Diesel products and the three
credits that could not be stored as Saleor variant prices. Every expected number
below is written out, never derived from the code under test.
"""

import pytest

from saleor.wsm.compose import pricing
from saleor.wsm.compose.pricing import (
    AboveRetailError,
    ComposeRefusal,
    Fee,
    MissingRequiredError,
    NegativeTotalError,
    OptionSet,
    Selection,
    TierDelta,
    UnknownValueError,
    Value,
    price_configured,
)

# The three credits, as their own single-choice option sets. Real shape: each is
# a separate group on the product, so any combination can be selected at once.
CAM_BEARINGS = -2999  # -29.99
PLUG_KIT = -3000  # -30.00
GASKET_SET = -44500  # -445.00

STAGE_2_BASE = 399899  # L600084, 3998.99
STAGE_3_BASE = 639900  # L600088, 6399.00


def credit_sets():
    return [
        OptionSet(
            id=1,
            name="Cam bearings",
            sort_order=1,
            values=(Value(id=11, name="Delete", sku_fragment="NCB", price_delta=CAM_BEARINGS),),
        ),
        OptionSet(
            id=2,
            name="Plug kit",
            sort_order=2,
            values=(Value(id=22, name="Delete", sku_fragment="NPK", price_delta=PLUG_KIT),),
        ),
        OptionSet(
            id=3,
            name="Gasket set",
            sort_order=3,
            values=(Value(id=33, name="Delete", sku_fragment="NGS", price_delta=GASKET_SET),),
        ),
    ]


def pick(*set_ids):
    return [Selection(set_id=i, value_ids=({1: 11, 2: 22, 3: 33}[i],)) for i in set_ids]


# Hand-computed from the base and the credit amounts in requirement 1.2. The two
# all-three rows (3494.00 and 5894.01) are the numbers the requirements doc pins.
@pytest.mark.parametrize(
    ("sku", "base", "chosen", "expected_cents"),
    [
        ("L600084", STAGE_2_BASE, (), 399899),
        ("L600084", STAGE_2_BASE, (1,), 396900),
        ("L600084", STAGE_2_BASE, (2,), 396899),
        ("L600084", STAGE_2_BASE, (3,), 355399),
        ("L600084", STAGE_2_BASE, (1, 2), 393900),
        ("L600084", STAGE_2_BASE, (1, 3), 352400),
        ("L600084", STAGE_2_BASE, (2, 3), 352399),
        ("L600084", STAGE_2_BASE, (1, 2, 3), 349400),
        ("L600088", STAGE_3_BASE, (), 639900),
        ("L600088", STAGE_3_BASE, (1,), 636901),
        ("L600088", STAGE_3_BASE, (2,), 636900),
        ("L600088", STAGE_3_BASE, (3,), 595400),
        ("L600088", STAGE_3_BASE, (1, 2), 633901),
        ("L600088", STAGE_3_BASE, (1, 3), 592401),
        ("L600088", STAGE_3_BASE, (2, 3), 592400),
        ("L600088", STAGE_3_BASE, (1, 2, 3), 589401),
    ],
)
def test_fixture_table(sku, base, chosen, expected_cents):
    result = price_configured(base, credit_sets(), pick(*chosen), base_sku=sku)
    assert result.unit_cents == expected_cents


def test_all_three_credits_sum_they_do_not_replace():
    """The multi-credit summation the dd60 report was filed about."""
    result = price_configured(STAGE_2_BASE, credit_sets(), pick(1, 2, 3), base_sku="L600084")
    assert result.unit_cents == 349400
    assert [line["price_delta"] for line in result.snapshot["lines"]] == [
        CAM_BEARINGS,
        PLUG_KIT,
        GASKET_SET,
    ]


def test_composite_sku_follows_set_order_not_selection_order():
    result = price_configured(
        STAGE_2_BASE, credit_sets(), pick(3, 1, 2), base_sku="L600084"
    )
    assert result.composite_sku == "L600084-NCB-NPK-NGS"


@pytest.mark.parametrize("base", [44500, 44400, 10])
def test_configured_unit_at_or_below_zero_refuses(base):
    """Requirement 1.3: never a free product, never clamped."""
    with pytest.raises(NegativeTotalError):
        price_configured(base, credit_sets(), pick(3))


def test_line_total_eaten_by_a_negative_fee_refuses():
    fee = Fee(id=1, label="Bad row", basis=pricing.FIXED, amount=-20000)
    with pytest.raises(NegativeTotalError) as caught:
        price_configured(10000, [], [], fees=[fee])
    assert caught.value.line is True


def test_tier_row_on_a_credit_is_verbatim_smaller_credit():
    """Requirement 2.2: a dealer credit of -300 against a retail credit of -445
    is the natural merchant intent and is taken as written."""
    sets = [
        OptionSet(
            id=3,
            name="Gasket set",
            values=(
                Value(
                    id=33,
                    name="Delete",
                    price_delta=GASKET_SET,
                    tier_deltas=(TierDelta(tier_group="dealer-1", price_delta=-30000),),
                ),
            ),
        )
    ]
    result = price_configured(STAGE_2_BASE, sets, pick(3), "dealer-1")
    assert result.unit_cents == 399899 - 30000
    assert result.snapshot["tier_applied"] is True


def test_tier_row_on_a_credit_is_verbatim_larger_credit():
    sets = [
        OptionSet(
            id=3,
            name="Gasket set",
            values=(
                Value(
                    id=33,
                    name="Delete",
                    price_delta=GASKET_SET,
                    tier_deltas=(TierDelta(tier_group="dealer-1", price_delta=-50000),),
                ),
            ),
        )
    ]
    assert price_configured(STAGE_2_BASE, sets, pick(3), "dealer-1").unit_cents == 349899


def positive_value_sets(tier_cents):
    return [
        OptionSet(
            id=3,
            name="Titanium",
            values=(
                Value(
                    id=33,
                    name="Titanium",
                    price_delta=10000,
                    tier_deltas=(TierDelta(tier_group="dealer-1", price_delta=tier_cents),),
                ),
            ),
        )
    ]


def test_above_retail_refuses_on_a_positive_delta():
    """Requirement 2.1: a tier is a dealer's discount, never a surcharge."""
    with pytest.raises(AboveRetailError):
        price_configured(STAGE_2_BASE, positive_value_sets(15000), pick(3), "dealer-1")


def test_below_retail_tier_on_a_positive_delta_is_charged():
    result = price_configured(STAGE_2_BASE, positive_value_sets(6000), pick(3), "dealer-1")
    assert result.unit_cents == 399899 + 6000


def test_no_tier_group_never_consults_tier_rows():
    result = price_configured(STAGE_2_BASE, positive_value_sets(15000), pick(3))
    assert result.unit_cents == 399899 + 10000
    assert result.snapshot["tier_applied"] is False


def test_tier_group_that_matches_nothing_prices_at_retail_and_says_so():
    result = price_configured(STAGE_2_BASE, positive_value_sets(6000), pick(3), "dealer-9")
    assert result.unit_cents == 399899 + 10000
    assert result.snapshot["tier_applied"] is False


def test_required_set_not_selected_refuses():
    sets = [
        OptionSet(
            id=1,
            name="Pump",
            label="Pump choice",
            required=True,
            values=(Value(id=11, name="Stock"),),
        )
    ]
    with pytest.raises(MissingRequiredError):
        price_configured(STAGE_2_BASE, sets, [])


def test_required_image_prompt_left_empty_refuses():
    """The 5.0 client renderer skipped image axes in its required check."""
    sets = [OptionSet(id=1, name="Logo", prompt_type="image", required=True)]
    with pytest.raises(MissingRequiredError):
        price_configured(STAGE_2_BASE, sets, [Selection(set_id=1, text="   ")])


def test_unknown_value_refuses():
    with pytest.raises(UnknownValueError):
        price_configured(STAGE_2_BASE, credit_sets(), [Selection(set_id=1, value_ids=(999,))])


def test_unknown_option_set_refuses():
    with pytest.raises(UnknownValueError):
        price_configured(STAGE_2_BASE, credit_sets(), [Selection(set_id=99, value_ids=(11,))])


def test_choice_one_takes_exactly_one_value():
    sets = [
        OptionSet(
            id=1,
            name="Pump",
            values=(Value(id=11, name="A"), Value(id=12, name="B")),
        )
    ]
    with pytest.raises(ComposeRefusal):
        price_configured(STAGE_2_BASE, sets, [Selection(set_id=1, value_ids=(11, 12))])


# 100.10 at 8.25%, quantity 3, is the case where the two scopes actually differ:
# per unit rounds 8.258... up to 8.26 three times (24.78); per line rounds
# 24.7747... down once (24.77).
@pytest.mark.parametrize(
    ("apply_to", "expected_fee_total"),
    [(pricing.PER_UNIT, 2478), (pricing.PER_LINE, 2477)],
)
def test_percent_fee_unit_versus_line(apply_to, expected_fee_total):
    fee = Fee(id=1, label="Handling", basis=pricing.PERCENT, amount=825, apply_to=apply_to)
    result = price_configured(10010, [], [], fees=[fee], quantity=3)
    assert result.unit_cents == 10010
    assert result.fee_total_cents == expected_fee_total


@pytest.mark.parametrize(
    ("apply_to", "expected_fee_total"), [(pricing.PER_UNIT, 7500), (pricing.PER_LINE, 2500)]
)
def test_fixed_fee_unit_versus_line(apply_to, expected_fee_total):
    fee = Fee(id=1, label="Crating", basis=pricing.FIXED, amount=2500, apply_to=apply_to)
    result = price_configured(10000, [], [], fees=[fee], quantity=3)
    assert result.fee_total_cents == expected_fee_total


def test_percent_fees_never_compound_on_each_other():
    fees = [
        Fee(id=1, label="A", basis=pricing.PERCENT, amount=1000),
        Fee(id=2, label="B", basis=pricing.PERCENT, amount=1000),
    ]
    result = price_configured(10000, [], [], fees=fees)
    assert result.fee_total_cents == 2000


def test_declinable_fee_is_not_charged_unless_accepted():
    fee = Fee(id=7, label="Liftgate", amount=5000, required=False, decline_label="No liftgate")
    assert price_configured(10000, [], [], fees=[fee]).fee_total_cents == 0
    accepted = price_configured(10000, [], [], fees=[fee], accepted_fee_ids=[7])
    assert accepted.fee_total_cents == 5000


def test_accepting_a_required_fee_refuses():
    fee = Fee(id=7, label="Crating", amount=5000, required=True)
    with pytest.raises(ComposeRefusal):
        price_configured(10000, [], [], fees=[fee], accepted_fee_ids=[7])


def test_accepting_a_fee_this_product_does_not_carry_refuses():
    with pytest.raises(UnknownValueError):
        price_configured(10000, [], [], fees=[], accepted_fee_ids=[7])


def test_fee_total_is_not_folded_into_the_unit_price():
    fee = Fee(id=1, label="Crating", amount=2500)
    result = price_configured(10000, [], [], fees=[fee])
    assert (result.unit_cents, result.fee_total_cents) == (10000, 2500)
