# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What a dealer LIST costs per row, which has to be nothing.

`WsmDealerGroup` carries two counts with no column behind them, and every list
that renders a group renders them: the group list itself, the customer list
(each customer names its group) and the tier grid. The group list annotates
them; the other two handed the type a bare related instance, so the fallback
`root.tier_prices.count()` ran twice per row. Twenty customers was 1+2N, and a
resolver that costs a query per row is invisible on a detail page and is the
whole request on a list.

The only assertion that catches that is the shape of the cost curve: the same
query count for two rows and for twenty. Sibling of
`saleor/wsm/graphql/containers/tests/test_list_query_cost.py`, same discipline.
"""

from decimal import Decimal

import pytest

from .....account.models import User
from .....graphql.tests.utils import get_graphql_content
from .....product.models import ProductVariant
from ....dealer.models import DealerCustomer, DealerGroup, TierPrice

pytestmark = pytest.mark.django_db


GROUP_LIST = """
    query List {
      wsmDealerGroups(first: 50) {
        edges { node { id code name tierPriceCount customerCount } }
      }
    }
"""

CUSTOMER_LIST = """
    query List {
      wsmDealerCustomers(first: 50) {
        edges {
          node {
            id taxExempt
            user { email }
            group { code tierPriceCount customerCount }
          }
        }
      }
    }
"""

TIER_LIST = """
    query List {
      wsmTierPrices(first: 50) {
        edges {
          node {
            id minQuantity amount currencyCode
            variant { sku }
            group { code tierPriceCount customerCount }
          }
        }
      }
    }
"""


def _groups(count, offset=0):
    return DealerGroup.objects.bulk_create(
        [
            DealerGroup(code=f"group-{index}", name=f"Group {index}")
            for index in range(offset, offset + count)
        ]
    )


def _customers(count, offset=0):
    group = DealerGroup.objects.first() or _groups(1, 9000)[0]
    users = User.objects.bulk_create(
        [
            User(email=f"dealer-{index}@example.com", is_active=True)
            for index in range(offset, offset + count)
        ]
    )
    return DealerCustomer.objects.bulk_create(
        [DealerCustomer(user=user, group=group) for user in users]
    )


def _tier_prices(product, count, offset=0):
    group = DealerGroup.objects.first() or _groups(1, 8000)[0]
    variants = ProductVariant.objects.bulk_create(
        [
            ProductVariant(product=product, sku=f"cost-{index}", name=str(index))
            for index in range(offset, offset + count)
        ]
    )
    return TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant,
                group=group,
                min_quantity=1,
                amount=Decimal("199.99"),
            )
            for variant in variants
        ]
    )


def _count(api_client, capture_queries, query):
    with capture_queries() as captured:
        response = api_client.post_graphql(query)
    get_graphql_content(response)
    return len(captured.captured_queries)


@pytest.mark.parametrize(
    ("query", "make"),
    [
        (GROUP_LIST, lambda product, count, offset: _groups(count, offset)),
        (CUSTOMER_LIST, lambda product, count, offset: _customers(count, offset)),
        (
            TIER_LIST,
            lambda product, count, offset: _tier_prices(product, count, offset),
        ),
    ],
    ids=["groups", "customers", "tier-prices"],
)
def test_the_list_costs_the_same_at_twenty_rows_as_at_two(
    staff_api_client,
    permission_manage_discounts,
    permission_manage_products,
    capture_queries,
    product,
    query,
    make,
):
    staff_api_client.user.user_permissions.add(
        permission_manage_discounts, permission_manage_products
    )
    make(product, 2, 0)
    # Discarded: the first request of a test warms per-process caches (site
    # settings, the permission lookup) that have nothing to do with row count.
    _count(staff_api_client, capture_queries, query)
    small = _count(staff_api_client, capture_queries, query)

    make(product, 18, 2)
    large = _count(staff_api_client, capture_queries, query)

    assert small == large, (
        f"{large - small} extra queries for 18 extra rows: the field is 1+N"
    )
