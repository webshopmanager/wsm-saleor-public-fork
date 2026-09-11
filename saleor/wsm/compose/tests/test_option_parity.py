# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The three things 5.0 option sets carry and the fork did not: parity wave A.

The fleet sweep of 2026-09-11 read all 124 go-live tenants and found 92 losing
something on migration. These are the three cheapest and earliest-biting, all
three on the value and set rows: the merchant's sentence under a choice
(`product_option_value.desc`, 83 tenants, 40,079 values), the pre-picked answer
(`product_option_value.default`, 76 tenants, 429 of them priced) and the wording
on the empty choice (`product_option_set.deselect`, 40 tenants, 8,879 sets).
Help text and the default both bite at row 1 of the go-live queue, `bda`, which
is live on 6.0 today.

Only the default is a PRICING fact, and that is what most of this file is
about: a priced default is money the quote opens with, so it is applied here and
not left to a browser to remember.
"""

import dataclasses
from decimal import Decimal

import pytest

from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    INVALID,
    OptionSet,
    OptionValue,
    duplicate_default_error,
)
from saleor.wsm.compose.pricing import (
    MissingRequiredError,
    Selection,
    Value,
    price_configured,
)

BASE = 10_000  # 100.00, so a delta is legible in the assertion


def _set(*values, required=False, prompt=pricing.CHOICE_ONE, set_id=1):
    return pricing.OptionSet(
        id=set_id, name="Tuner", prompt_type=prompt, required=required, values=values
    )


PLAIN = Value(id=11, name="No tuner", price_delta=0, sort_order=0)
PRICED_DEFAULT = Value(
    id=12, name="Stage 2 tuner", price_delta=25_000, is_default=True, sort_order=1
)


# --- the default, priced, applied where the money is taken -----------------


def test_an_omitted_optional_set_quotes_the_merchants_default():
    """The wave's whole reason: 429 defaults across the fleet carry a price."""
    priced = price_configured(BASE, [_set(PLAIN, PRICED_DEFAULT)], [])

    assert priced.unit_cents == BASE + 25_000
    assert [line["value_id"] for line in priced.snapshot["lines"]] == [12]


def test_an_omitted_optional_set_with_no_default_quotes_nothing():
    """The check above has to be able to fail: same call, one flag cleared."""
    priced = price_configured(
        BASE, [_set(PLAIN, dataclasses.replace(PRICED_DEFAULT, is_default=False))], []
    )

    assert priced.unit_cents == BASE
    assert priced.snapshot["lines"] == []


def test_a_shopper_who_declines_an_optional_set_is_not_charged_its_default():
    """Saying no and saying nothing are different answers.

    An empty selection is the deselect prompt being clicked. Without this the
    priced default could never be refused, because saying nothing quotes it.
    """
    priced = price_configured(
        BASE, [_set(PLAIN, PRICED_DEFAULT)], [Selection(set_id=1, value_ids=())]
    )

    assert priced.unit_cents == BASE
    assert priced.snapshot["lines"] == []


def test_a_shopper_who_picks_another_answer_is_not_charged_the_default():
    priced = price_configured(
        BASE, [_set(PLAIN, PRICED_DEFAULT)], [Selection(set_id=1, value_ids=(11,))]
    )

    assert priced.unit_cents == BASE


def test_a_required_set_with_a_default_still_refuses_an_unanswered_add():
    """Fail closed. The default is what the storefront OPENS on, not consent.

    Applying it here would sell the merchant's guess to a caller who never
    rendered the question.
    """
    with pytest.raises(MissingRequiredError):
        price_configured(BASE, [_set(PLAIN, PRICED_DEFAULT, required=True)], [])


def test_a_pick_any_number_set_never_pre_ticks_a_box():
    """No single answer to pre-pick.

    Filling one in would charge for a box nobody ticked.
    """
    priced = price_configured(
        BASE, [_set(PLAIN, PRICED_DEFAULT, prompt=pricing.CHOICE_MANY)], []
    )

    assert priced.unit_cents == BASE


def test_two_defaults_price_to_the_first_in_catalog_order_and_never_raise():
    """Rows written past both merchant screens must not break a buy button."""
    second = Value(
        id=13, name="Stage 3", price_delta=40_000, is_default=True, sort_order=2
    )

    priced = price_configured(BASE, [_set(PLAIN, PRICED_DEFAULT, second)], [])

    assert priced.unit_cents == BASE + 25_000


def test_a_required_choice_one_still_takes_exactly_one():
    """Relaxing the optional case must not open the required one."""
    with pytest.raises(pricing.ComposeRefusal):
        price_configured(
            BASE,
            [_set(PLAIN, PRICED_DEFAULT, required=True)],
            [Selection(set_id=1, value_ids=())],
        )


def test_no_choice_one_set_ever_takes_two_values():
    with pytest.raises(pricing.ComposeRefusal):
        price_configured(
            BASE,
            [_set(PLAIN, PRICED_DEFAULT)],
            [Selection(set_id=1, value_ids=(11, 12))],
        )


# --- the rows themselves ---------------------------------------------------


def test_the_three_columns_round_trip_through_the_model(product, db):
    option_set = OptionSet.objects.create(
        product=product,
        name="Tuner",
        prompt_type=pricing.CHOICE_ONE,
        deselect_prompt="Skip the tuner",
    )
    OptionValue.objects.create(option_set=option_set, name="No tuner", sort_order=0)
    default = OptionValue.objects.create(
        option_set=option_set,
        name="Stage 2 tuner",
        price_delta=Decimal("250.00"),
        help_text="Fits 2019 and newer only",
        is_default=True,
        sort_order=1,
    )

    stored = OptionSet.objects.get(pk=option_set.pk)
    assert stored.deselect_prompt == "Skip the tuner"
    assert stored.default_value == default
    assert stored.default_value.help_text == "Fits 2019 and newer only"
    assert stored.to_pricing().values[1].is_default is True
    assert stored.to_pricing().values[0].is_default is False


def test_the_default_is_read_in_catalog_order_not_insert_order(product, db):
    option_set = OptionSet.objects.create(
        product=product, name="Tuner", prompt_type=pricing.CHOICE_ONE
    )
    late = OptionValue.objects.create(
        option_set=option_set, name="Stage 3", is_default=True, sort_order=9
    )
    early = OptionValue.objects.create(
        option_set=option_set, name="Stage 2", is_default=True, sort_order=1
    )

    assert late.pk < early.pk
    assert option_set.default_value == early


def test_the_duplicate_default_sentence_carries_a_code_the_dashboard_declares():
    error = duplicate_default_error("Stage 2 tuner")

    assert error.code == INVALID
    assert "Stage 2 tuner" in error.message
