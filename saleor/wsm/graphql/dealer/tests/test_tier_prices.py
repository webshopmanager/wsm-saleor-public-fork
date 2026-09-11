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
from .....product.models import ProductVariant, ProductVariantChannelListing
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

BULK_CREATE_GRID = """
    mutation BulkCreate($tierPrices: [WsmTierPriceBulkCreateInput!]!) {
      wsmTierPriceBulkCreate(tierPrices: $tierPrices) {
        count
        tierPrices { id amount currencyCode variant { sku product { name } } }
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


def paste_variants(product, channel, count, offset):
    """`count` SKUs, each listed in one channel, because the grid labels money."""
    variants = ProductVariant.objects.bulk_create(
        [
            ProductVariant(product=product, sku=f"grid-{index}", name=str(index))
            for index in range(offset, offset + count)
        ]
    )
    ProductVariantChannelListing.objects.bulk_create(
        [
            ProductVariantChannelListing(
                variant=variant,
                channel=channel,
                currency=channel.currency_code,
                price_amount=Decimal("199.99"),
            )
            for variant in variants
        ]
    )
    return variants


def paste_cost(client, capture_queries, group, variants):
    rows = [
        {
            "variant": variant_gid(variant),
            "group": group_gid(group),
            "minQuantity": 1,
            "amount": "199.99",
        }
        for variant in variants
    ]
    with capture_queries() as captured:
        response = client.post_graphql(BULK_CREATE_GRID, {"tierPrices": rows})
    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkCreate"]
    assert payload["errors"] == []
    assert payload["count"] == len(variants)
    return len(captured.captured_queries)


def test_a_price_leaves_as_an_exact_string_and_not_as_a_float(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """`PositiveDecimal` is a `graphene.Float` with no `serialize` override.

    So the money this domain returns went out through IEEE double: 2.50 became
    2.5, and a screen that posts back what it was handed is one rounding step
    away from charging a different price than the merchant typed.
    """
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount="2.50",
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"] == []
    assert payload["tierPrice"]["amount"] == "2.50"

    row = TierPrice.objects.get()
    # No `permissions=`: the create above already granted it, and that argument
    # asserts the call is refused without it first.
    content = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": tier_gid(row)})
    )
    # Read back off the column, which holds three places to match
    # CheckoutLine.price_override. Still a string, still exact, and quantized to
    # the two places a price has: the grid re-sends this value on a quantity
    # edit, and "2.500" came back as a refusal the merchant never earned.
    assert content["data"]["wsmTierPrice"]["amount"] == "2.50"


def test_a_negative_price_is_refused_as_a_price_and_not_as_a_missing_one(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """-1 is a price the merchant typed, not a price they forgot to type.

    `PositiveDecimal.parse_value` maps anything below zero to None
    (`saleor/graphql/core/scalars.py:56-62`), so the amount rule never saw the
    number and answered REQUIRED: "say what this group pays", about a field the
    merchant had just filled in.
    """
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount=-1,
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"] == [
        {
            "field": "amount",
            "code": "TIER_AMOUNT_BELOW_ONE_CENT",
            "message": "a dealer price is a price, so it is at least one cent",
        }
    ]
    assert not TierPrice.objects.exists()


def test_the_paste_response_costs_the_same_at_three_hundred_rows_as_at_twenty(
    staff_api_client,
    permission_manage_discounts,
    product,
    channel_USD,
    dealer_group,
    capture_queries,
):
    """The rows this mutation returns are the rows the Dashboard renders.

    It selects `variant.product.name` and `currencyCode` on each one, and
    `bulk_create` hands back bare instances, so the RESPONSE was a query per
    line of the paste even though validating it was not. The only assertion
    that catches that is the shape of the cost curve.
    """
    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    # Discarded: the first request of a test warms per-process caches (site
    # settings, the permission lookup) that have nothing to do with row count.
    paste_cost(
        staff_api_client,
        capture_queries,
        dealer_group,
        paste_variants(product, channel_USD, 1, 0),
    )

    small = paste_cost(
        staff_api_client,
        capture_queries,
        dealer_group,
        paste_variants(product, channel_USD, 20, 100),
    )
    large = paste_cost(
        staff_api_client,
        capture_queries,
        dealer_group,
        paste_variants(product, channel_USD, 300, 1000),
    )

    assert large == small, f"20 rows cost {small} queries, 300 cost {large}"


def test_the_currency_is_the_cheapest_listing_and_null_when_there_is_none(
    staff_api_client,
    permission_manage_discounts,
    product,
    channel_USD,
    channel_PLN,
    dealer_group,
):
    """One money box, one label, and no label at all when there is no price.

    The resolver took whichever listing came back first, so the column heading
    on a two-channel SKU depended on row order, and a SKU in no channel got an
    arbitrary Channel's currency: a box labelled in a currency that SKU is not
    sold in, which is worse than an empty label.
    """
    listed = ProductVariant.objects.create(product=product, sku="two-channels")
    ProductVariantChannelListing.objects.create(
        variant=listed,
        channel=channel_USD,
        currency="USD",
        price_amount=Decimal("100.00"),
    )
    ProductVariantChannelListing.objects.create(
        variant=listed,
        channel=channel_PLN,
        currency="PLN",
        price_amount=Decimal("5.00"),
    )
    unlisted = ProductVariant.objects.create(product=product, sku="no-channel")
    rows = TierPrice.objects.bulk_create(
        [
            TierPrice(variant=listed, group=dealer_group, min_quantity=1, amount="90"),
            TierPrice(
                variant=unlisted, group=dealer_group, min_quantity=1, amount="90"
            ),
        ]
    )
    staff_api_client.user.user_permissions.add(permission_manage_discounts)

    cheapest = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": tier_gid(rows[0])})
    )
    assert cheapest["data"]["wsmTierPrice"]["currencyCode"] == "PLN"

    nowhere = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": tier_gid(rows[1])})
    )
    assert nowhere["data"]["wsmTierPrice"]["currencyCode"] is None


def test_a_paste_above_the_cap_is_refused_before_a_row_is_written(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """An uncapped bulk create is a merchant paste away from a held-open request.

    Stock caps its own bulk create the same way
    (`MAX_ORDERS`, `saleor/graphql/order/bulk_mutations/order_bulk_create.py:86`),
    and the refusal carries stock's code for it so the screen can say which
    limit was hit rather than "invalid".
    """
    rows = [
        {
            "variant": variant_gid(variant),
            "group": group_gid(dealer_group),
            "minQuantity": quantity,
            "amount": "10.00",
        }
        for quantity in range(1, 502)
    ]

    response = staff_api_client.post_graphql(
        BULK_CREATE_COUNT_ONLY,
        {"tierPrices": rows},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkCreate"]
    assert payload["count"] == 0
    assert payload["errors"] == [
        {
            "field": "tierPrices",
            "code": "BULK_LIMIT",
            "message": "501 rows in one call, and the limit is 500. Split the paste.",
        }
    ]
    assert not TierPrice.objects.exists()


# --- wave 2A: stored precision, indexed errors, the bulk update --------------


BULK_UPDATE = """
    mutation BulkUpdate($tierPrices: [WsmTierPriceBulkUpdateInput!]!) {
      wsmTierPriceBulkUpdate(tierPrices: $tierPrices) {
        count
        tierPrices { id minQuantity amount }
        errors { field code message }
      }
    }
"""

BULK_UPDATE_COUNT_ONLY = """
    mutation BulkUpdate($tierPrices: [WsmTierPriceBulkUpdateInput!]!) {
      wsmTierPriceBulkUpdate(tierPrices: $tierPrices) {
        count
        errors { field code message }
      }
    }
"""

BULK_UPDATE_GRID = """
    mutation BulkUpdate($tierPrices: [WsmTierPriceBulkUpdateInput!]!) {
      wsmTierPriceBulkUpdate(tierPrices: $tierPrices) {
        count
        tierPrices { id amount currencyCode variant { sku product { name } } }
        errors { field code message }
      }
    }
"""


def test_a_stored_price_leaves_the_api_at_the_two_places_a_merchant_typed():
    """The column holds three places; a price is two. The wire is the price.

    `Decimal("270.000")` is what the column gives back, and it left the API as
    the string "270.000". The Dashboard grid then posts that string back on a
    quantity edit and the amount rule refused a number the merchant never typed.
    Formatting on the screen is the screen's job, but the API should not emit
    the noise in the first place.
    """
    from ...scalars import WsmDecimal

    assert WsmDecimal.serialize(Decimal("270.000")) == "270.00"
    assert WsmDecimal.serialize(Decimal("119.990")) == "119.99"
    assert WsmDecimal.serialize(None) is None


def test_a_tier_price_reads_back_as_two_decimals(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="270.000"
    )

    content = get_graphql_content(
        staff_api_client.post_graphql(
            DETAIL, {"id": tier_gid(row)}, permissions=[permission_manage_discounts]
        )
    )
    assert content["data"]["wsmTierPrice"]["amount"] == "270.00"


def test_a_quantity_edit_does_not_refuse_the_price_the_api_handed_back(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """The blocker: every existing row refused to save once its row was touched.

    The grid re-sends `amount` unchanged when the merchant edits the quantity.
    The amount that came out of the API carried the column's third place, and
    the rule counted decimal places rather than asking what would be CHARGED,
    so the merchant was told their own untouched price had too many decimals.
    """
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="270.000"
    )

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": tier_gid(row), "input": {"minQuantity": 10, "amount": "270.000"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceUpdate"]
    assert payload["errors"] == []
    assert payload["tierPrice"]["amount"] == "270.00"
    row.refresh_from_db()
    assert row.amount == Decimal("270.00")
    assert row.min_quantity == 10


def test_a_trailing_zero_on_the_cents_is_not_a_third_decimal_place(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount="119.990",
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceCreate"]
    assert payload["errors"] == []
    assert payload["tierPrice"]["amount"] == "119.99"
    assert TierPrice.objects.get().amount == Decimal("119.99")


def test_a_real_half_cent_is_still_refused(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """The rule this fix must not soften: 119.995 is precision nobody can pay."""
    response = create(
        staff_api_client,
        permission_manage_discounts,
        variant,
        dealer_group,
        amount="119.995",
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


def test_the_single_update_names_the_cell_the_merchant_is_typing_in(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """The Dashboard maps `field` to a grid cell, so a blank field is a blank row."""
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    bad_amount = get_graphql_content(
        staff_api_client.post_graphql(
            UPDATE,
            {"id": tier_gid(row), "input": {"amount": "0"}},
            permissions=[permission_manage_discounts],
        )
    )["data"]["wsmTierPriceUpdate"]
    assert [error["field"] for error in bad_amount["errors"]] == ["amount"]

    bad_quantity = get_graphql_content(
        staff_api_client.post_graphql(
            UPDATE, {"id": tier_gid(row), "input": {"minQuantity": 0}}
        )
    )["data"]["wsmTierPriceUpdate"]
    assert [error["field"] for error in bad_quantity["errors"]] == ["minQuantity"]


# --- bulk update, the grid's save-all path ----------------------------------


def test_the_bulk_update_is_refused_without_the_permission(
    staff_api_client, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE_COUNT_ONLY,
        {"tierPrices": [{"id": tier_gid(row), "amount": "90.00"}]},
    )

    assert_no_permission(response)
    assert TierPrice.objects.get().amount == Decimal("100.000")


def test_a_bulk_update_changes_every_row_it_names(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    rows = TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=dealer_group, min_quantity=quantity, amount="100"
            )
            for quantity in (1, 10, 25)
        ]
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE,
        {
            "tierPrices": [
                {"id": tier_gid(rows[0]), "amount": "95.00"},
                {"id": tier_gid(rows[1]), "minQuantity": 12},
                {"id": tier_gid(rows[2]), "amount": "80.00", "minQuantity": 30},
            ]
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"] == []
    assert payload["count"] == 3
    assert [row["amount"] for row in payload["tierPrices"]] == [
        "95.00",
        "100.00",
        "80.00",
    ]
    assert sorted(TierPrice.objects.values_list("min_quantity", flat=True)) == [
        1,
        12,
        30,
    ]


def test_a_bulk_update_row_that_only_moves_the_quantity_keeps_its_stored_price(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    """A quantity-only edit on a three-place stored amount is the live case."""
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="270.000"
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE,
        {"tierPrices": [{"id": tier_gid(row), "minQuantity": 6}]},
        permissions=[permission_manage_discounts],
    )

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"] == []
    assert payload["tierPrices"][0]["amount"] == "270.00"
    row.refresh_from_db()
    assert row.min_quantity == 6
    assert row.amount == Decimal("270.00")


def test_a_bad_bulk_update_row_names_its_own_line_and_nothing_is_written(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    rows = TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=dealer_group, min_quantity=quantity, amount="100"
            )
            for quantity in (1, 10)
        ]
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE,
        {
            "tierPrices": [
                {"id": tier_gid(rows[0]), "amount": "95.00"},
                {"id": tier_gid(rows[1]), "amount": "0.001"},
            ]
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmTierPriceBulkUpdate"]
    assert payload["count"] == 0
    assert payload["errors"] == [
        {
            "field": "tierPrices.1.amount",
            "code": "TIER_AMOUNT_TOO_MANY_DECIMALS",
            "message": "a price has at most two decimal places",
        }
    ]
    assert sorted(TierPrice.objects.values_list("amount", flat=True)) == [
        Decimal("100.000"),
        Decimal("100.000"),
    ]


def test_a_bulk_update_row_naming_a_row_that_is_not_there_names_its_line(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )
    gone = tier_gid(row)
    row.delete()

    response = staff_api_client.post_graphql(
        BULK_UPDATE_COUNT_ONLY,
        {"tierPrices": [{"id": gone, "amount": "95.00"}]},
        permissions=[permission_manage_discounts],
    )

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"][0]["field"] == "tierPrices.0.id"
    assert payload["errors"][0]["code"] == "NOT_FOUND"


def test_a_bulk_update_cannot_move_two_rows_onto_one_break(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    rows = TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=dealer_group, min_quantity=quantity, amount="100"
            )
            for quantity in (1, 10)
        ]
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE_COUNT_ONLY,
        {
            "tierPrices": [
                {"id": tier_gid(rows[0]), "minQuantity": 5},
                {"id": tier_gid(rows[1]), "minQuantity": 5},
            ]
        },
        permissions=[permission_manage_discounts],
    )

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"][0]["field"] == "tierPrices.1.minQuantity"
    assert payload["errors"][0]["code"] == "DUPLICATE_TIER_BREAK"
    assert sorted(TierPrice.objects.values_list("min_quantity", flat=True)) == [1, 10]


def test_a_bulk_update_cannot_move_a_row_onto_a_stored_break(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    rows = TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=dealer_group, min_quantity=quantity, amount="100"
            )
            for quantity in (1, 10)
        ]
    )

    response = staff_api_client.post_graphql(
        BULK_UPDATE_COUNT_ONLY,
        {"tierPrices": [{"id": tier_gid(rows[0]), "minQuantity": 10}]},
        permissions=[permission_manage_discounts],
    )

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"][0]["field"] == "tierPrices.0.minQuantity"
    assert payload["errors"][0]["code"] == "DUPLICATE_TIER_BREAK"
    assert TierPrice.objects.get(pk=rows[0].pk).min_quantity == 1


def test_a_bulk_update_above_the_cap_is_refused_before_a_row_is_written(
    staff_api_client, permission_manage_discounts, variant, dealer_group
):
    row = TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="100.00"
    )
    rows = [{"id": tier_gid(row), "amount": "10.00"} for _ in range(501)]

    response = staff_api_client.post_graphql(
        BULK_UPDATE_COUNT_ONLY,
        {"tierPrices": rows},
        permissions=[permission_manage_discounts],
    )

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["count"] == 0
    assert payload["errors"] == [
        {
            "field": "tierPrices",
            "code": "BULK_LIMIT",
            "message": "501 rows in one call, and the limit is 500. Split the paste.",
        }
    ]
    assert TierPrice.objects.get().amount == Decimal("100.000")


def test_three_hundred_rows_are_updated_in_one_statement(
    staff_api_client, permission_manage_discounts, product, dealer_group, capsys
):
    """The grid's save-all button, measured. One UPDATE and a flat query count.

    A loop of `wsmTierPriceUpdate` is 300 round trips and four figures of
    queries to save one screen; this asserts the whole grid is ONE statement
    and that validating it costs a fixed number of queries rather than one per
    row.
    """
    variants = ProductVariant.objects.bulk_create(
        [
            ProductVariant(product=product, sku=f"grid-save-{index}", name=str(index))
            for index in range(300)
        ]
    )
    stored = TierPrice.objects.bulk_create(
        [
            TierPrice(
                variant=variant, group=dealer_group, min_quantity=1, amount="199.99"
            )
            for variant in variants
        ]
    )
    rows = [
        {"id": tier_gid(row), "amount": "189.99", "minQuantity": 2} for row in stored
    ]

    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = staff_api_client.post_graphql(
            BULK_UPDATE_COUNT_ONLY, {"tierPrices": rows}
        )
    elapsed = time.perf_counter() - started

    payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
    assert payload["errors"] == []
    assert payload["count"] == 300

    updates = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE")
        and "wsm_dealer_tierprice" in query["sql"]
    ]
    with capsys.disabled():
        # The measurement IS the point of this test; ruff's no-print rule is
        # about production code.
        print(  # noqa: T201
            f"\n[2A bulkUpdate 300 rows] queries={len(queries.captured_queries)} "
            f"tier-price UPDATEs={len(updates)} wall={elapsed * 1000:.0f} ms"
        )
    assert len(updates) == 1
    assert len(queries.captured_queries) < 25
    assert set(TierPrice.objects.values_list("min_quantity", flat=True)) == {2}
    assert set(TierPrice.objects.values_list("amount", flat=True)) == {
        Decimal("189.990")
    }


def test_the_bulk_update_response_costs_the_same_at_three_hundred_rows_as_at_twenty(
    staff_api_client,
    permission_manage_discounts,
    product,
    channel_USD,
    dealer_group,
    capture_queries,
):
    """The rows it returns are the rows the grid re-renders, so they are loaded."""
    staff_api_client.user.user_permissions.add(permission_manage_discounts)

    def cost(count, offset):
        variants = paste_variants(product, channel_USD, count, offset)
        stored = TierPrice.objects.bulk_create(
            [
                TierPrice(
                    variant=variant, group=dealer_group, min_quantity=1, amount="199.99"
                )
                for variant in variants
            ]
        )
        rows = [{"id": tier_gid(row), "amount": "189.99"} for row in stored]
        with capture_queries() as captured:
            response = staff_api_client.post_graphql(
                BULK_UPDATE_GRID, {"tierPrices": rows}
            )
        payload = get_graphql_content(response)["data"]["wsmTierPriceBulkUpdate"]
        assert payload["errors"] == []
        assert payload["count"] == count
        return len(captured.captured_queries)

    # Discarded: the first request of a test warms per-process caches.
    cost(1, 9000)
    small = cost(20, 9100)
    large = cost(300, 9200)

    assert large == small, f"20 rows cost {small} queries, 300 cost {large}"
