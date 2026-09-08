# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from decimal import Decimal

import graphene
import pytest

from ..models import DealerCustomer, DealerGroup, TierPrice
from ..pricing import MAX_BATCH, dealer_price_for, prices_for_variants

pytestmark = pytest.mark.django_db


@pytest.fixture
def dealer_group(customer_user):
    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    return group


def ladder(variant, group, rows):
    TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=group, min_quantity=qty, amount=Decimal(amount)
            )
            for qty, amount in rows
        ]
    )


@pytest.mark.parametrize(
    ("quantity", "expected_amount", "expected_break"),
    [(1, "9.00", 1), (4, "9.00", 1), (5, "8.00", 5), (9, "8.00", 5), (12, "7.00", 10)],
)
def test_highest_reachable_break_wins(
    variant, customer_user, dealer_group, channel_USD, quantity, expected_amount, expected_break
):
    ladder(variant, dealer_group, [(1, "9.00"), (5, "8.00"), (10, "7.00")])

    amount, min_quantity, group_code = dealer_price_for(
        variant, customer_user, quantity, channel=channel_USD
    )

    assert amount == Decimal(expected_amount)
    assert min_quantity == expected_break
    assert group_code == "dealer-1"


def test_quantity_under_the_lowest_break_has_no_dealer_price(
    variant, customer_user, dealer_group, channel_USD
):
    ladder(variant, dealer_group, [(5, "8.00")])

    assert dealer_price_for(variant, customer_user, 4, channel=channel_USD) is None


def test_a_tier_above_retail_is_never_offered(
    variant, customer_user, dealer_group, channel_USD
):
    # The variant fixture lists at 10.00, so the 12.00 row is a merchant data
    # bug: a dealer never pays more than retail.
    ladder(variant, dealer_group, [(1, "12.00"), (5, "8.00")])

    assert dealer_price_for(variant, customer_user, 1, channel=channel_USD) is None
    assert dealer_price_for(variant, customer_user, 5, channel=channel_USD).amount == Decimal(8)

    gid = graphene.Node.to_global_id("ProductVariant", variant.pk)
    assert prices_for_variants(customer_user, channel_USD, [variant.pk]) == {
        gid: [{"minQuantity": 5, "amount": "8.00"}]
    }


def test_a_shopper_who_is_not_a_dealer_gets_nothing(
    variant, customer_user, dealer_group, channel_USD, staff_user
):
    ladder(variant, dealer_group, [(1, "9.00")])

    assert dealer_price_for(variant, staff_user, 1, channel=channel_USD) is None
    assert prices_for_variants(staff_user, channel_USD, [variant.pk]) == {}


def test_batch_is_one_query_and_carries_the_whole_ladder(
    variant, customer_user, dealer_group, channel_USD, django_assert_num_queries
):
    ladder(variant, dealer_group, [(1, "9.00"), (10, "7.00")])
    gid = graphene.Node.to_global_id("ProductVariant", variant.pk)

    # One query for the whole page: the buyer's group is reached by JOIN and the
    # retail floor rides along as a subquery, so 100 variants cost what 1 does.
    with django_assert_num_queries(1):
        breaks = prices_for_variants(customer_user, channel_USD, [variant.pk] * 3)

    assert breaks == {
        gid: [
            {"minQuantity": 1, "amount": "9.00"},
            {"minQuantity": 10, "amount": "7.00"},
        ]
    }
    assert MAX_BATCH == 100
