# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The option-set screens, over the wire: four mutations and two query fields.

The rules asserted here are the admin's own, reached through GraphQL rather
than re-invented: the configured floor checked across the WHOLE submitted list
(`compose/forms.py:236`), the duplicate SKU fragment, the duplicate tier group,
and the dealer delta that would price a buyer group above retail. What is new
is only the shape they arrive in: a `field` the Dashboard can attach to an
input, and a `code` it can branch on.
"""

from decimal import Decimal

import graphene
import pytest

from saleor.graphql.tests.utils import assert_no_permission, get_graphql_content
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    OptionSet,
    OptionValue,
)
from saleor.wsm.graphql.utils import BULK_LIMIT

DETAIL = """
    query WsmOptionSet($id: ID!) {
      wsmOptionSet(id: $id) {
        id
        name
        label
        promptType
        required
        sortOrder
        currencyCode
        product { id }
        values {
          id
          name
          skuFragment
          priceDelta
          sortOrder
          tierDeltas { id tierGroup priceDelta dealerGroup { code name } }
        }
      }
    }
"""

LIST = """
    query WsmOptionSets($filter: WsmOptionSetFilterInput) {
      wsmOptionSets(first: 20, filter: $filter) {
        totalCount
        edges { node { id name } }
      }
    }
"""

CREATE = """
    mutation WsmOptionSetCreate($input: WsmOptionSetCreateInput!) {
      wsmOptionSetCreate(input: $input) {
        optionSet { id name values { id name priceDelta } }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation WsmOptionSetUpdate($id: ID!, $input: WsmOptionSetUpdateInput!) {
      wsmOptionSetUpdate(id: $id, input: $input) {
        optionSet {
          id
          name
          values { id name priceDelta tierDeltas { tierGroup priceDelta } }
        }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation WsmOptionSetDelete($id: ID!) {
      wsmOptionSetDelete(id: $id) {
        optionSet { id }
        errors { field code message }
      }
    }
"""

BULK_DELETE = """
    mutation WsmOptionSetBulkDelete($ids: [ID!]!) {
      wsmOptionSetBulkDelete(ids: $ids) {
        count
        errors { field code message }
      }
    }
"""


def set_id(row):
    return graphene.Node.to_global_id("WsmOptionSet", row.pk)


def value_id(row):
    return graphene.Node.to_global_id("WsmOptionValue", row.pk)


def product_id(product):
    return graphene.Node.to_global_id("Product", product.pk)


# --- reads -------------------------------------------------------------------


@pytest.mark.django_db
def test_an_anonymous_caller_is_refused(api_client, option_set):
    response = api_client.post_graphql(DETAIL, {"id": set_id(option_set)})

    assert_no_permission(response)


@pytest.mark.django_db
def test_a_staff_token_without_manage_products_is_refused(staff_api_client, option_set):
    response = staff_api_client.post_graphql(DETAIL, {"id": set_id(option_set)})

    assert_no_permission(response)


@pytest.mark.django_db
def test_the_detail_query_returns_the_question_its_answers_and_the_tier_deltas(
    merchant_api_client, option_set, tier_delta
):
    response = merchant_api_client.post_graphql(DETAIL, {"id": set_id(option_set)})

    data = get_graphql_content(response)["data"]["wsmOptionSet"]
    assert data["name"] == "Finish"
    assert data["label"] == "Choose a finish"
    assert data["promptType"] == "CHOICE_ONE"
    assert data["currencyCode"] == "USD", "a money input with no currency on it"
    assert [value["name"] for value in data["values"]] == ["Black", "Raw"]
    # SIGNED: a credit is a negative delta, which is why the scalar is Decimal
    # and not PositiveDecimal.
    assert data["values"][1]["priceDelta"] == "-1.00", "signed, and exact"
    deltas = data["values"][0]["tierDeltas"]
    assert deltas == [
        {
            "id": graphene.Node.to_global_id("WsmDealerTierOptionPrice", tier_delta.pk),
            "tierGroup": "dealer-1",
            "priceDelta": "5.00",
            "dealerGroup": {"code": "dealer-1", "name": "Dealer tier 1"},
        }
    ]


@pytest.mark.django_db
def test_a_tier_delta_whose_group_was_deleted_still_renders(
    merchant_api_client, option_set, tier_delta, dealer_group
):
    """The column is a CharField, so the row outlives the group it names.

    The screen has to show the stored code with no group beside it; a resolver
    that raised here would take the whole option-set page down over one
    dangling string.
    """
    dealer_group.delete()

    response = merchant_api_client.post_graphql(DETAIL, {"id": set_id(option_set)})

    data = get_graphql_content(response)["data"]["wsmOptionSet"]
    delta = data["values"][0]["tierDeltas"][0]
    assert delta["tierGroup"] == "dealer-1"
    assert delta["dealerGroup"] is None


@pytest.mark.django_db
def test_the_list_filters_by_product(merchant_api_client, option_set, product_list):
    other = OptionSet.objects.create(product=product_list[0], name="Pump wiring")

    response = merchant_api_client.post_graphql(
        LIST, {"filter": {"product": product_id(option_set.product)}}
    )

    data = get_graphql_content(response)["data"]["wsmOptionSets"]
    names = [edge["node"]["name"] for edge in data["edges"]]
    assert names == ["Finish"], f"{other.name} leaked across products"
    assert data["totalCount"] == 1


@pytest.mark.django_db
def test_the_list_search_reaches_the_products_own_sku(
    merchant_api_client, option_set, product_list
):
    """A merchant looking for the question on a part types the PART number."""
    OptionSet.objects.create(product=product_list[0], name="Pump wiring")
    sku = option_set.product.variants.first().sku

    response = merchant_api_client.post_graphql(LIST, {"filter": {"search": sku}})

    data = get_graphql_content(response)["data"]["wsmOptionSets"]
    assert [edge["node"]["name"] for edge in data["edges"]] == ["Finish"]


# --- wsmOptionSetCreate ------------------------------------------------------


@pytest.mark.django_db
def test_create_is_refused_without_the_permission(staff_api_client, product):
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": {"product": product_id(product), "name": "Finish"}},
    )

    assert_no_permission(response)
    assert not OptionSet.objects.exists()


@pytest.mark.django_db
def test_create_stores_the_question_and_its_answers(merchant_api_client, product):
    response = merchant_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "product": product_id(product),
                "name": "Finish",
                "label": "Choose a finish",
                "promptType": "CHOICE_ONE",
                "required": True,
                "values": [
                    {"name": "Black", "skuFragment": "BLK", "priceDelta": "10.00"},
                    {"name": "Raw", "skuFragment": "RAW", "priceDelta": "-1.00"},
                ],
            }
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetCreate"]
    assert payload["errors"] == []
    assert [value["name"] for value in payload["optionSet"]["values"]] == [
        "Black",
        "Raw",
    ]
    stored = OptionSet.objects.get()
    assert stored.required is True
    assert stored.prompt_type == pricing.CHOICE_ONE
    assert stored.values.get(name="Raw").price_delta == Decimal("-1.00")


@pytest.mark.django_db
def test_create_refuses_a_credit_the_product_cannot_carry_and_stores_nothing(
    merchant_api_client, product
):
    """The floor rule, on the first save of a brand new question.

    A credit deeper than the product's own price makes the configured price
    negative, which the buy button refuses with a message written for a
    developer. The merchant hears about it here instead, and the question is
    not left half-created: create and its nested list are one transaction.
    """
    response = merchant_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "product": product_id(product),
                "name": "Delete the engine",
                "promptType": "CHOICE_ONE",
                "required": True,
                "values": [{"name": "Yes please", "priceDelta": "-999999.00"}],
            }
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetCreate"]
    assert payload["optionSet"] is None
    assert [error["code"] for error in payload["errors"]] == [
        "CONFIGURED_FLOOR_BELOW_ZERO"
    ]
    assert payload["errors"][0]["field"] == "values"
    assert not OptionSet.objects.exists(), "a refused create left a row behind"
    assert not OptionValue.objects.exists()


# --- wsmOptionSetUpdate ------------------------------------------------------


@pytest.mark.django_db
def test_update_is_refused_without_the_permission(staff_api_client, option_set):
    response = staff_api_client.post_graphql(
        UPDATE, {"id": set_id(option_set), "input": {"name": "Renamed"}}
    )

    assert_no_permission(response)
    option_set.refresh_from_db()
    assert option_set.name == "Finish"


@pytest.mark.django_db
def test_update_replaces_the_whole_answer_list(merchant_api_client, option_set, black):
    """Provided means provided: a row left out of the list is a row deleted.

    The Dashboard's datagrid sends what the merchant is looking at. A merge
    would leave a deleted choice on the storefront and no screen would show it.
    """
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {"id": value_id(black), "name": "Black", "priceDelta": "12.00"}
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert payload["errors"] == []
    assert [value["name"] for value in payload["optionSet"]["values"]] == ["Black"]
    assert list(option_set.values.values_list("name", flat=True)) == ["Black"]
    black.refresh_from_db()
    assert black.price_delta == Decimal("12.00")


@pytest.mark.django_db
def test_update_leaves_the_answers_alone_when_values_is_omitted(
    merchant_api_client, option_set
):
    response = merchant_api_client.post_graphql(
        UPDATE, {"id": set_id(option_set), "input": {"name": "Finish and colour"}}
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert payload["errors"] == []
    option_set.refresh_from_db()
    assert option_set.name == "Finish and colour"
    assert option_set.values.count() == 2


@pytest.mark.django_db
def test_update_accepts_the_submit_that_fixes_two_stored_rows_at_once(
    merchant_api_client, option_set, product
):
    """The reason the list is replaced whole and checked whole.

    Both stored credits are deeper than the product can carry. A row-wise check
    compares the row being fixed against the sibling still stored broken and
    refuses the very submit that fixes them, which is the bug the admin moved
    to the formset (`compose/forms.py:236`). The merchant submits both corrected
    rows together and the check is asked of the edit as it would stand.
    """
    option_set.required = True
    option_set.save()
    broken = [
        OptionValue.objects.create(
            option_set=option_set, name="Half off", price_delta=Decimal("-9999.00")
        ),
        OptionValue.objects.create(
            option_set=option_set, name="Free", price_delta=Decimal("-9999.00")
        ),
    ]

    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(broken[0]),
                        "name": "Half off",
                        "priceDelta": "-2.00",
                    },
                    {"id": value_id(broken[1]), "name": "Free", "priceDelta": "-3.00"},
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert payload["errors"] == []
    assert sorted(
        str(value) for value in option_set.values.values_list("price_delta", flat=True)
    ) == ["-2.00", "-3.00"]


@pytest.mark.django_db
def test_update_refuses_dealer_prices_from_a_catalog_manager(
    merchant_api_client, option_set, black, dealer_group
):
    """MANAGE_PRODUCTS opens the question. It does not open what a dealer pays.

    `tierDeltas` writes `WsmDealerTierOptionPrice`, the same money every
    mutation in `wsm/graphql/dealer` gates on MANAGE_DISCOUNTS.
    """
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [{"tierGroup": "dealer-1", "priceDelta": "5.00"}],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas", "PERMISSION_DENIED")
    ]
    assert not DealerTierOptionPrice.objects.exists(), "a refused save wrote money"


@pytest.mark.django_db
def test_update_refuses_a_catalog_manager_CLEARING_dealer_prices(
    merchant_api_client, option_set, black, tier_delta
):
    """An empty list is a write too: provided REPLACES, so `[]` deletes."""
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas", "PERMISSION_DENIED")
    ]
    assert DealerTierOptionPrice.objects.filter(pk=tier_delta.pk).exists()


@pytest.mark.django_db
def test_create_refuses_dealer_prices_from_a_catalog_manager(
    merchant_api_client, product, dealer_group
):
    """The same input object rides on create, so it needs the same gate."""
    response = merchant_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "product": product_id(product),
                "name": "Finish",
                "values": [
                    {
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [{"tierGroup": "dealer-1", "priceDelta": "5.00"}],
                    }
                ],
            }
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetCreate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas", "PERMISSION_DENIED")
    ]
    assert not OptionSet.objects.exists(), "a refused create left a question behind"


@pytest.mark.django_db
def test_update_stores_a_dealer_price_for_a_caller_who_has_the_permission(
    dealer_merchant_api_client, option_set, black, dealer_group
):
    """The gate refuses a permission, not the feature."""
    response = dealer_merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [{"tierGroup": "dealer-1", "priceDelta": "5.00"}],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert payload["errors"] == []
    assert payload["optionSet"]["values"][0]["tierDeltas"] == [
        {"tierGroup": "dealer-1", "priceDelta": "5.00"}
    ]


@pytest.mark.django_db
def test_update_refuses_two_answers_sharing_one_sku_code(
    merchant_api_client, option_set
):
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {"name": "Black", "skuFragment": "BLK"},
                    {"name": "Midnight", "skuFragment": "BLK"},
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.1.skuFragment", "DUPLICATE_SKU_FRAGMENT")
    ]
    assert option_set.values.count() == 2, "a refused update rewrote the list"
    assert set(option_set.values.values_list("name", flat=True)) == {"Black", "Raw"}


@pytest.mark.django_db
def test_update_refuses_two_prices_for_one_dealer_group(
    dealer_merchant_api_client, option_set, black, dealer_group
):
    """Checked before the write, because the constraint fires as a 500.

    `wsm_compose_one_tier_row_per_group` is real, and reaching it means an
    IntegrityError on a merchant screen instead of a sentence on a field.
    """
    response = dealer_merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [
                            {"tierGroup": "dealer-1", "priceDelta": "5.00"},
                            {"tierGroup": "dealer-1", "priceDelta": "4.00"},
                        ],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas.1.tierGroup", "DUPLICATE_TIER_GROUP")
    ]
    assert option_set.values.count() == 2, "a refused update deleted the other answer"


@pytest.mark.django_db
def test_update_refuses_a_tier_group_no_dealer_group_answers_to(
    dealer_merchant_api_client, option_set, black, dealer_group
):
    response = dealer_merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [
                            {"tierGroup": "warehouse", "priceDelta": "5.00"}
                        ],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas.0.tierGroup", "UNKNOWN_DEALER_GROUP")
    ]


@pytest.mark.django_db
def test_update_refuses_a_dealer_price_above_retail(
    dealer_merchant_api_client, option_set, black, dealer_group
):
    response = dealer_merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": set_id(option_set),
            "input": {
                "values": [
                    {
                        "id": value_id(black),
                        "name": "Black",
                        "priceDelta": "10.00",
                        "tierDeltas": [
                            {"tierGroup": "dealer-1", "priceDelta": "20.00"}
                        ],
                    }
                ]
            },
        },
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("values.0.tierDeltas.0.priceDelta", "DEALER_DELTA_ABOVE_RETAIL")
    ]
    assert not black.tier_deltas.exists()


# --- wsmOptionSetDelete ------------------------------------------------------


@pytest.mark.django_db
def test_delete_is_refused_without_the_permission(staff_api_client, option_set):
    """An ungated delete on a public API is the whole catalogue, one call."""
    response = staff_api_client.post_graphql(DELETE, {"id": set_id(option_set)})

    assert_no_permission(response)
    assert OptionSet.objects.filter(pk=option_set.pk).exists()


@pytest.mark.django_db
def test_delete_removes_the_question_and_every_answer_on_it(
    merchant_api_client, option_set
):
    response = merchant_api_client.post_graphql(DELETE, {"id": set_id(option_set)})

    payload = get_graphql_content(response)["data"]["wsmOptionSetDelete"]
    assert payload["errors"] == []
    assert payload["optionSet"]["id"] == set_id(option_set)
    assert not OptionSet.objects.exists()
    assert not OptionValue.objects.exists()


# --- wsmOptionSetBulkDelete --------------------------------------------------


@pytest.mark.django_db
def test_bulk_delete_is_refused_without_the_permission(staff_api_client, option_set):
    response = staff_api_client.post_graphql(BULK_DELETE, {"ids": [set_id(option_set)]})

    assert_no_permission(response)
    assert OptionSet.objects.filter(pk=option_set.pk).exists()


@pytest.mark.django_db
def test_bulk_delete_removes_only_the_rows_it_was_given(
    merchant_api_client, option_set, product_list
):
    spared = OptionSet.objects.create(product=product_list[0], name="Pump wiring")

    response = merchant_api_client.post_graphql(
        BULK_DELETE, {"ids": [set_id(option_set)]}
    )

    payload = get_graphql_content(response)["data"]["wsmOptionSetBulkDelete"]
    assert payload["errors"] == []
    assert payload["count"] == 1
    assert list(OptionSet.objects.values_list("pk", flat=True)) == [spared.pk]


@pytest.mark.django_db
def test_bulk_delete_refuses_more_ids_than_the_cap(merchant_api_client, option_set):
    """Bounded before a single id is decoded, and nothing is deleted."""
    ids = [set_id(option_set)] * (BULK_LIMIT + 1)

    response = merchant_api_client.post_graphql(BULK_DELETE, {"ids": ids})

    payload = get_graphql_content(response)["data"]["wsmOptionSetBulkDelete"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("ids", "BULK_LIMIT")
    ]
    assert payload["count"] == 0
    assert OptionSet.objects.filter(pk=option_set.pk).exists()
