# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Tier prices over GraphQL, through the real view.

A tier price is money that gets CHARGED, so the three tests that matter here are
not CRUD:

- The one-cent floor. `wsm_dealer_tier_amount_at_least_a_cent` is a database
  CheckConstraint, so a mutation that let a bad row reach it hands the merchant
  a 500 instead of a sentence. Both halves are asserted: the mutation refuses
  with its own code, and the table still refuses anything that skips it.
- Two decimal places. The column holds three to match
  `CheckoutLine.price_override`; nobody means a tenth of a cent, so the screen
  refuses one rather than rounding it away.
- The 300-row paste. This is the import path, and the test below measures it:
  one INSERT, not three hundred.
"""

import time
from decimal import Decimal

import graphene
import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from .....product.models import ProductVariant
from ....dealer.models import DealerGroup, TierPrice

pytestmark = pytest.mark.django_db


CREATE = """
    mutation Create($input: WsmTierPriceCreateInput!) {
      wsmTierPriceCreate(input: $input) {
        tierPrice {
          id minQuantity amount currencyCode
          variant { id sku }
          group { code }
        }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation Update($id: ID!, $input: WsmTierPriceUpdateInput!) {
      wsmTierPriceUpdate(id: $id, input: $input) {
        tierPrice { id minQuantity amount }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation Delete($id: ID!) {
      wsmTierPriceDelete(id: $id) {
        tierPrice { id }
        errors { field code message }
      }
    }
"""

BULK_DELETE = """
    mutation BulkDelete($ids: [ID!]!) {
      wsmTierPriceBulkDelete(ids: $ids) {
        count
        errors { field code message }
      }
    }
"""

BULK_CREATE = """
    mutation BulkCreate($tierPrices: [WsmTierPriceBulkCreateInput!]!) {
      wsmTierPriceBulkCreate(tierPrices: $tierPrices) {
        count
        tierPrices { id minQuantity amount }
        errors { field code message }
      }
    }
"""

BULK_CREATE_COUNT_ONLY = """
    mutation BulkCreate($tierPrices: [WsmTierPriceBulkCreateInput!]!) {
      wsmTierPriceBulkCreate(tierPrices: $tierPrices) {
        count
        errors { field code message }
      }
    }
"""

DETAIL = """
    query Detail($id: ID!) {
      wsmTierPrice(id: $id) {
        id minQuantity amount currencyCode
        variant { sku }
        group { code }
      }
    }
"""

LIST = """
    query List($filter: WsmTierPriceFilterInput) {
      wsmTierPrices(filter: $filter, first: 100) {
        totalCount
        edges { node { id minQuantity amount currencyCode variant { sku } } }
      }
    }
"""


def group_gid(group):
    return graphene.Node.to_global_id("WsmDealerGroup", group.pk)


def variant_gid(variant):
    return graphene.Node.to_global_id("ProductVariant", variant.pk)


def tier_gid(row):
    return graphene.Node.to_global_id("WsmTierPrice", row.pk)


@pytest.fixture
def dealer_group(db):
    return DealerGroup.objects.create(code="dealer-1", name="Dealer tier 1")


def create(client, permission, variant, group, **overrides):
    payload = {
        "variant": variant_gid(variant),
        "group": group_gid(group),
        "minQuantity": 1,
        "amount": "228.00",
    }
    payload.update(overrides)
    return client.post_graphql(CREATE, {"input": payload}, permissions=[permission])


# --- permissions -------------------------------------------------------------


def test_an_anonymous_caller_cannot_read_a_tier_price(
    api_client, variant, dealer_group
):
    row = TierPrice.objects.create(variant=variant, group=dealer_group, amount="10.00")

    response = api_client.post_graphql(DETAIL, {"id": tier_gid(row)})

    assert_no_permission(response)


def test_manage_products_alone_cannot_read_a_tier_price(
    staff_api_client, variant, dealer_group, permission_manage_products
):
    row = TierPrice.objects.create(variant=variant, group=dealer_group, amount="10.00")

    response = staff_api_client.post_graphql(
        DETAIL, {"id": tier_gid(row)}, permissions=[permission_manage_products]
    )

    assert_no_permission(response)


def test_the_create_is_refused_without_the_permission(
    staff_api_client, variant, dealer_group
):
    response = staff_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "variant": variant_gid(variant),
                "group": group_gid(dealer_group),
                "amount": "10.00",
            }
        },
    )

    assert_no_permission(response)
    assert not TierPrice.objects.exists()


def test_the_bulk_create_is_refused_without_the_permission(
    staff_api_client, variant, dealer_group
):
    response = staff_api_client.post_graphql(
        BULK_CREATE,
        {
            "tierPrices": [
                {
                    "variant": variant_gid(variant),
                    "group": group_gid(dealer_group),
                    "amount": "10.00",
                }
            ]
        },
    )

    assert_no_permission(response)
    assert not TierPrice.objects.exists()


# --- create ------------------------------------------------------------------


def test_creating_a_tier_price_stores_what_the_group_pays(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    response = create(
        staff_api_client, permission_manage_discounts, variant, dealer_group
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"] == []
    assert payload["tierPrice"]["variant"]["sku"] == variant.sku
    assert payload["tierPrice"]["group"]["code"] == "dealer-1"
    assert Decimal(payload["tierPrice"]["amount"]) == Decimal("228.00")
    assert payload["tierPrice"]["currencyCode"] == "USD"
    assert TierPrice.objects.get().amount == Decimal("228.000")


def test_an_amount_below_one_cent_is_a_field_error_not_a_server_error(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """The CheckConstraint's rule, refused where a merchant can still fix it.

    A tier price is an ABSOLUTE price, so a zero one is not a big discount, it
    is a line that charges nothing. `wsm_dealer_tier_amount_at_least_a_cent`
    would refuse the row at the database and take the request with it; this is
    the same refusal as a sentence on the field that is wrong.
    """
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount="0",
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["tierPrice"] is None
    assert payload["errors"] == [
        {
            "field": "amount",
            "code": "TIER_AMOUNT_BELOW_ONE_CENT",
            "message": "a dealer price is a price, so it is at least one cent",
        }
    ]
    assert not TierPrice.objects.exists()


def test_the_table_still_refuses_a_sub_cent_amount_under_any_other_writer(
    variant, dealer_group
):
    """The backstop the mutation stands on, asserted so it cannot be dropped.

    If this constraint ever went away, the field error above would be the ONLY
    thing between a 5.0 importer and a line that charges nothing.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        TierPrice.objects.create(
            variant=variant, group=dealer_group, min_quantity=1, amount=Decimal("0.004")
        )


def test_a_third_decimal_place_is_refused_rather_than_rounded_away(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount="228.125",
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"] == [
        {
            "field": "amount",
            "code": "TIER_AMOUNT_TOO_MANY_DECIMALS",
            "message": "a price has at most two decimal places",
        }
    ]
    assert not TierPrice.objects.exists()


def test_one_group_cannot_have_two_prices_for_the_same_break(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = create(
        staff_api_client, permission_manage_discounts, variant, dealer_group
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"][0]["code"] == "DUPLICATE_TIER_BREAK"
    assert TierPrice.objects.get().amount == Decimal("100.000")


# --- update ------------------------------------------------------------------


def test_updating_changes_the_price_and_the_break(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": tier_gid(row), "input": {"minQuantity": 10, "amount": "90.50"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceUpdate"]
    assert payload["errors"] == []
    row.refresh_from_db()
    assert row.min_quantity == 10
    assert row.amount == Decimal("90.500")


def test_an_update_cannot_move_a_row_onto_another_break(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=10, amount="90.00"
    )
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": tier_gid(row), "input": {"minQuantity": 10}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    assert (
        content["data"]["wsmTierPriceUpdate"]["errors"][0]["code"]
        == "DUPLICATE_TIER_BREAK"
    )
    row.refresh_from_db()
    assert row.min_quantity == 1


# --- delete ------------------------------------------------------------------


def test_deleting_a_break_leaves_the_sku_alone(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        DELETE, {"id": tier_gid(row)}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmTierPriceDelete"]["errors"] == []
    assert not TierPrice.objects.exists()
    assert ProductVariant.objects.filter(pk=variant.pk).exists()


def test_bulk_delete_clears_a_selection(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    rows = [
        TierPrice.objects.create(
            variant=variant, group=dealer_group, min_quantity=quantity, amount="90.00"
        )
        for quantity in (1, 10, 25)
    ]

    response = staff_api_client.post_graphql(
        BULK_DELETE,
        {"ids": [tier_gid(rows[0]), tier_gid(rows[1])]},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkDelete"]
    assert payload["errors"] == []
    assert payload["count"] == 2
    assert list(TierPrice.objects.values_list("min_quantity", flat=True)) == [25]


# --- bulk create, the paste path ---------------------------------------------


def test_a_paste_creates_every_row_in_one_call(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    response = staff_api_client.post_graphql(
        BULK_CREATE,
        {
            "tierPrices": [
                {
                    "variant": variant_gid(variant),
                    "group": group_gid(dealer_group),
                    "minQuantity": quantity,
                    "amount": amount,
                }
                for quantity, amount in ((1, "100.00"), (10, "90.00"), (25, "80.00"))
            ]
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["errors"] == []
    assert payload["count"] == 3
    assert [Decimal(row["amount"]) for row in payload["tierPrices"]] == [
        Decimal("100.00"),
        Decimal("90.00"),
        Decimal("80.00"),
    ]
    assert TierPrice.objects.count() == 3


def test_a_bad_row_names_its_own_line_and_the_whole_paste_is_refused(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """One transaction: a paste that is half right lands nothing.

    A merchant fixes line 2 of their spreadsheet and pastes again; a paste that
    had already written line 1 would then collide with itself.
    """
    response = staff_api_client.post_graphql(
        BULK_CREATE,
        {
            "tierPrices": [
                {
                    "variant": variant_gid(variant),
                    "group": group_gid(dealer_group),
                    "minQuantity": 1,
                    "amount": "100.00",
                },
                {
                    "variant": variant_gid(variant),
                    "group": group_gid(dealer_group),
                    "minQuantity": 10,
                    "amount": "0.001",
                },
            ]
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["count"] == 0
    assert payload["errors"] == [
        {
            "field": "tierPrices.1.amount",
            "code": "TIER_AMOUNT_TOO_MANY_DECIMALS",
            "message": "a price has at most two decimal places",
        }
    ]
    assert not TierPrice.objects.exists()


def test_a_paste_that_repeats_a_break_within_itself_is_refused(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = {
        "variant": variant_gid(variant),
        "group": group_gid(dealer_group),
        "minQuantity": 1,
        "amount": "100.00",
    }

    response = staff_api_client.post_graphql(
        BULK_CREATE,
        {"tierPrices": [row, row]},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["errors"][0]["field"] == "tierPrices.1.minQuantity"
    assert payload["errors"][0]["code"] == "DUPLICATE_TIER_BREAK"
    assert not TierPrice.objects.exists()


def test_a_paste_that_repeats_a_stored_break_is_refused(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        BULK_CREATE,
        {
            "tierPrices": [
                {
                    "variant": variant_gid(variant),
                    "group": group_gid(dealer_group),
                    "minQuantity": 1,
                    "amount": "95.00",
                }
            ]
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["errors"][0]["code"] == "DUPLICATE_TIER_BREAK"
    assert TierPrice.objects.get().amount == Decimal("100.000")


def test_three_hundred_rows_cost_one_insert(
    staff_api_client, permission_manage_discounts, product, dealer_group, capsys
):
    """The measurement the exit bar names, as an assertion rather than a note.

    Live data is 304+ tier rows on one group (`dealer/admin.py:32`), so the
    paste path is the shape this domain is actually used in. A loop of
    `wsmTierPriceCreate` would be 300 round trips; this asserts the whole
    spreadsheet is ONE insert and that validating it costs a fixed number of
    queries rather than a query per line.
    """
    variants = ProductVariant.objects.bulk_create(
        [
            ProductVariant(product=product, sku=f"paste-{index}", name=str(index))
            for index in range(300)
        ]
    )
    rows = [
        {
            "variant": variant_gid(variant),
            "group": group_gid(dealer_group),
            "minQuantity": 1,
            "amount": "199.99",
        }
        for variant in variants
    ]

    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = staff_api_client.post_graphql(
            BULK_CREATE_COUNT_ONLY, {"tierPrices": rows}
        )
    elapsed = time.perf_counter() - started

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["errors"] == []
    assert payload["count"] == 300
    assert TierPrice.objects.count() == 300

    inserts = [
        query["sql"]
        for query in queries.captured_queries
        if "INSERT INTO" in query["sql"] and "wsm_dealer_tierprice" in query["sql"]
    ]
    with capsys.disabled():
        # The measurement IS the point of this test; ruff's no-print rule is
        # about production code.
        print(  # noqa: T201
            f"\n[U4 bulkCreate 300 rows] queries={len(queries.captured_queries)} "
            f"tier-price INSERTs={len(inserts)} wall={elapsed * 1000:.0f} ms"
        )
    assert len(inserts) == 1
    # The whole request, auth and all. A per-row validation would be 300+.
    assert len(queries.captured_queries) < 25


# --- reads -------------------------------------------------------------------


def test_the_tier_list_filters_by_product_because_a_product_is_many_skus(
    staff_api_client,
    permission_manage_discounts,
    product,
    product_with_two_variants,
    dealer_group,
):
    mine = product.variants.first()
    other = product_with_two_variants.variants.first()
    TierPrice.objects.create(
        variant=mine, group=dealer_group, min_quantity=1, amount="100.00"
    )
    TierPrice.objects.create(
        variant=other, group=dealer_group, min_quantity=1, amount="50.00"
    )

    response = staff_api_client.post_graphql(
        LIST,
        {"filter": {"product": graphene.Node.to_global_id("Product", product.pk)}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    skus = [
        edge["node"]["variant"]["sku"]
        for edge in content["data"]["wsmTierPrices"]["edges"]
    ]
    assert skus == [mine.sku]


def test_the_tier_list_carries_the_currency_the_money_column_is_labelled_with(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        LIST, {}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    node = content["data"]["wsmTierPrices"]["edges"][0]["node"]
    assert node["currencyCode"] == "USD"
