# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What a container LIST costs per row, which has to be nothing.

`memberCount` and `currencyCode` are aggregates with no column behind them, and
the Dashboard list screens select both. A resolver that runs its own query is
invisible on a detail page and is 1+N on a list of 100, so the only assertion
that catches it is the shape of the cost curve: the same query count for two
rows and for twenty.
"""

import pytest

from .....graphql.tests.utils import get_graphql_content
from .....product.models import Collection
from ....containers.models import KitConfig, SeriesConfig

pytestmark = pytest.mark.django_db


SERIES_LIST = """
    query List {
      wsmSeriesConfigs(first: 50) {
        edges { node { id brand memberCount } }
      }
    }
"""

KIT_LIST = """
    query List {
      wsmKitConfigs(first: 50) {
        edges { node { id currencyCode } }
      }
    }
"""


def _collections(count, offset=0):
    return Collection.objects.bulk_create(
        [
            Collection(name=f"Series {index}", slug=f"series-{index}")
            for index in range(offset, offset + count)
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
        (
            SERIES_LIST,
            lambda rows: SeriesConfig.objects.bulk_create(
                [
                    SeriesConfig(collection=collection, brand="WeatherTech", axes=[])
                    for collection in rows
                ]
            ),
        ),
        (
            KIT_LIST,
            lambda rows: KitConfig.objects.bulk_create(
                [KitConfig(collection=collection) for collection in rows]
            ),
        ),
    ],
    ids=["series-member-count", "kit-currency-code"],
)
def test_the_list_costs_the_same_at_twenty_rows_as_at_two(
    staff_api_client, permission_manage_products, capture_queries, query, make
):
    staff_api_client.user.user_permissions.add(permission_manage_products)
    make(_collections(2))
    # Discarded: the first request of a test warms per-process caches (site
    # settings, the permission lookup) that have nothing to do with row count
    # and would otherwise show up as three queries the small list "saved".
    _count(staff_api_client, capture_queries, query)
    small = _count(staff_api_client, capture_queries, query)

    make(_collections(18, offset=2))
    large = _count(staff_api_client, capture_queries, query)

    assert small == large, (
        f"{large - small} extra queries for 18 extra rows: the field is 1+N"
    )
