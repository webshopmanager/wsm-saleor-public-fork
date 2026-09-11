# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The Compose tab on the stock product page: three fields, one round trip.

`product_extension.py` appends the fields to stock's `Product` type from our own
module, so `saleor/graphql/product/types/products.py` is never edited. Two
things have to be true for that to be cheap, and both are asserted here: the
injection does not leak into the schema stock builds for itself, and the tab
costs a CONSTANT number of queries however many products are on the page.
"""

from contextlib import ExitStack
from decimal import Decimal

import graphene
import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext

from saleor.graphql import api
from saleor.graphql.tests.utils import assert_no_permission, get_graphql_content
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    Fee,
    OptionSet,
    OptionValue,
    ProductCompliance,
)
from saleor.wsm.graphql import schema as wsm_schema
from saleor.wsm.graphql.compose.product_extension import WSM_PRODUCT_FIELDS

ONE_PRODUCT = """
    query Product($id: ID!) {
      product(id: $id) {
        id
        wsmOptionSets { id name values { id name priceDelta tierDeltas { tierGroup } } }
        wsmFees { id label amount }
        wsmCompliance { id prop65 restrictedStates }
      }
    }
"""

THE_TAB_ON_A_LIST = """
    query Products($first: Int!, $channel: String!) {
      products(first: $first, channel: $channel) {
        edges {
          node {
            id
            wsmOptionSets {
              id
              values { id priceDelta tierDeltas { tierGroup priceDelta } }
            }
            wsmFees { id label amount }
            wsmCompliance { id prop65 restrictedStates }
          }
        }
      }
    }
"""


def compose_rows_on(product, dealer_group):
    """One of everything the tab renders, so no loader is asked for nothing."""
    option_set = OptionSet.objects.create(
        product=product, name="Finish", prompt_type=pricing.CHOICE_ONE
    )
    value = OptionValue.objects.create(
        option_set=option_set, name="Black", price_delta=Decimal("10.00")
    )
    DealerTierOptionPrice.objects.create(
        option_value=value, tier_group=dealer_group.code, price_delta=Decimal("5.00")
    )
    Fee.objects.create(
        product=product,
        label="Freight crating",
        basis=pricing.FIXED,
        amount=Decimal("149.00"),
    )
    ProductCompliance.objects.create(
        product=product, prop65=True, restricted_states="CA"
    )


def count_queries(call):
    """Queries on every alias, because the reads route to the replica."""
    with ExitStack() as stack:
        contexts = [
            stack.enter_context(CaptureQueriesContext(connections[alias]))
            for alias in connections
        ]
        call()
        return sum(len(context) for context in contexts)


def test_the_three_fields_are_on_our_product_type_and_not_on_stocks():
    """Composition, not mutation. Stock's own schema never grew a WSM field."""
    ours = wsm_schema.schema.get_type_map()["Product"].fields
    theirs = api.schema.get_type_map()["Product"].fields

    assert WSM_PRODUCT_FIELDS == ("wsm_option_sets", "wsm_fees", "wsm_compliance")
    for name in ("wsmOptionSets", "wsmFees", "wsmCompliance"):
        assert name in ours
        assert name not in theirs


@pytest.mark.django_db
def test_the_tab_is_refused_to_a_staff_token_without_manage_products(
    staff_api_client, product, dealer_group
):
    compose_rows_on(product, dealer_group)

    response = staff_api_client.post_graphql(
        ONE_PRODUCT, {"id": graphene.Node.to_global_id("Product", product.pk)}
    )

    assert_no_permission(response)


@pytest.mark.django_db
def test_the_product_page_carries_the_questions_charges_and_disclosure(
    merchant_api_client, product, dealer_group
):
    compose_rows_on(product, dealer_group)

    response = merchant_api_client.post_graphql(
        ONE_PRODUCT, {"id": graphene.Node.to_global_id("Product", product.pk)}
    )

    data = get_graphql_content(response)["data"]["product"]
    assert [row["name"] for row in data["wsmOptionSets"]] == ["Finish"]
    assert data["wsmOptionSets"][0]["values"][0]["tierDeltas"] == [
        {"tierGroup": "dealer-1"}
    ]
    assert [row["label"] for row in data["wsmFees"]] == ["Freight crating"]
    assert data["wsmCompliance"]["restrictedStates"] == ["CA"]


@pytest.mark.django_db
def test_the_compose_tab_costs_the_same_for_one_product_and_for_three(
    merchant_api_client, product_list, dealer_group, channel_USD
):
    """Constant in N, not a pinned number.

    Three levels of relation (`wsmOptionSets { values { tierDeltas } }`) on a
    LIST of products is a three-level N+1 resolved naively. A pinned count would
    go stale on the next upstream bump; what matters is that the second product
    is free.
    """
    for row in product_list:
        compose_rows_on(row, dealer_group)

    def ask(first):
        response = merchant_api_client.post_graphql(
            THE_TAB_ON_A_LIST, {"first": first, "channel": channel_USD.slug}
        )
        content = get_graphql_content(response)
        assert len(content["data"]["products"]["edges"]) == first
        return content

    ask(1)  # warm: the first request of a session pays for the user and the site
    one = count_queries(lambda: ask(1))
    three = count_queries(lambda: ask(3))

    assert one > 0, "nothing was measured"
    assert three == one, f"{three} queries for three products against {one} for one"
