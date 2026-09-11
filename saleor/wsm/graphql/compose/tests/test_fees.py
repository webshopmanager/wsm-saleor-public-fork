# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The charge screens, over the wire: four mutations and two query fields.

One column, two meanings: `amount` is money when the basis is FIXED and a
percentage when it is PERCENT. Both rules that guard it are the model's own
(`compose/models.py:1108`), and what this file proves is that they arrive as a
field error on the input the merchant is typing in rather than as a 500.
"""

from decimal import Decimal

import graphene
import pytest

from saleor.graphql.tests.utils import assert_no_permission, get_graphql_content
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import Fee

DETAIL = """
    query WsmFee($id: ID!) {
      wsmFee(id: $id) {
        id
        label
        sku
        basis
        amount
        applyTo
        required
        declineLabel
        currencyCode
        variant { id }
        product { id }
      }
    }
"""

LIST = """
    query WsmFees($filter: WsmFeeFilterInput) {
      wsmFees(first: 20, filter: $filter) {
        totalCount
        edges { node { id label } }
      }
    }
"""

CREATE = """
    mutation WsmFeeCreate($input: WsmFeeCreateInput!) {
      wsmFeeCreate(input: $input) {
        fee { id label amount basis applyTo }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation WsmFeeUpdate($id: ID!, $input: WsmFeeUpdateInput!) {
      wsmFeeUpdate(id: $id, input: $input) {
        fee { id label amount }
        errors { field code message }
      }
    }
"""

# The carrier variant is deliberately absent from the update input. This
# document names it, so it is a QUERY VALIDATION failure for as long as that
# holds, and a silently accepted write the day somebody adds the field.
UPDATE_NAMING_THE_VARIANT = """
    mutation WsmFeeUpdate($id: ID!, $variant: ID!) {
      wsmFeeUpdate(id: $id, input: {variant: $variant}) {
        fee { id }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation WsmFeeDelete($id: ID!) {
      wsmFeeDelete(id: $id) {
        fee { id }
        errors { field code message }
      }
    }
"""

BULK_DELETE = """
    mutation WsmFeeBulkDelete($ids: [ID!]!) {
      wsmFeeBulkDelete(ids: $ids) {
        count
        errors { field code message }
      }
    }
"""


def fee_id(row):
    return graphene.Node.to_global_id("WsmFee", row.pk)


def product_id(product):
    return graphene.Node.to_global_id("Product", product.pk)


# --- reads -------------------------------------------------------------------


@pytest.mark.django_db
def test_an_anonymous_caller_is_refused(api_client, fee):
    response = api_client.post_graphql(DETAIL, {"id": fee_id(fee)})

    assert_no_permission(response)


@pytest.mark.django_db
def test_the_detail_query_returns_the_charge(merchant_api_client, fee):
    response = merchant_api_client.post_graphql(DETAIL, {"id": fee_id(fee)})

    data = get_graphql_content(response)["data"]["wsmFee"]
    assert data["label"] == "Freight crating"
    assert data["sku"] == "CRATE"
    assert data["basis"] == "FIXED"
    assert data["amount"] == "149.00", "exact, and not a float"
    assert data["applyTo"] == "PER_LINE"
    assert data["currencyCode"] == "USD"
    assert data["variant"] is None, "the carrier variant is written on first buy"


@pytest.mark.django_db
def test_the_list_filters_by_product(merchant_api_client, fee, product_list):
    Fee.objects.create(
        product=product_list[0],
        label="Hazmat handling",
        basis=pricing.FIXED,
        amount=Decimal("25.00"),
    )

    response = merchant_api_client.post_graphql(
        LIST, {"filter": {"product": product_id(fee.product)}}
    )

    data = get_graphql_content(response)["data"]["wsmFees"]
    assert [edge["node"]["label"] for edge in data["edges"]] == ["Freight crating"]
    assert data["totalCount"] == 1


# --- wsmFeeCreate ------------------------------------------------------------


@pytest.mark.django_db
def test_create_is_refused_without_the_permission(staff_api_client, product):
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": {"product": product_id(product), "label": "Crating", "amount": 149}},
    )

    assert_no_permission(response)
    assert not Fee.objects.exists()


@pytest.mark.django_db
def test_create_stores_the_charge(merchant_api_client, product):
    response = merchant_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "product": product_id(product),
                "label": "Freight crating",
                "sku": "CRATE",
                "basis": "FIXED",
                "amount": 149,
                "applyTo": "PER_LINE",
                "required": True,
            }
        },
    )

    payload = get_graphql_content(response)["data"]["wsmFeeCreate"]
    assert payload["errors"] == []
    # Two places, not "149" as typed: a money field leaves this API as the
    # amount that would be CHARGED (`WsmDecimal.serialize` -> `to_money`), so a
    # screen that posts back what it was handed posts back a legal price.
    assert payload["fee"]["amount"] == "149.00"
    stored = Fee.objects.get()
    assert stored.apply_to == pricing.PER_LINE
    assert stored.amount == Decimal("149.00")


@pytest.mark.django_db
def test_create_refuses_a_percentage_above_one_hundred_and_stores_nothing(
    merchant_api_client, product
):
    """A percentage over 100 more than doubles the price, every time by slip."""
    response = merchant_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "product": product_id(product),
                "label": "Handling",
                "basis": "PERCENT",
                "amount": 825,
            }
        },
    )

    payload = get_graphql_content(response)["data"]["wsmFeeCreate"]
    assert payload["fee"] is None
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("amount", "FEE_PERCENT_ABOVE_100")
    ]
    assert not Fee.objects.exists()


# --- wsmFeeUpdate ------------------------------------------------------------


@pytest.mark.django_db
def test_update_is_refused_without_the_permission(staff_api_client, fee):
    response = staff_api_client.post_graphql(
        UPDATE, {"id": fee_id(fee), "input": {"amount": 1}}
    )

    assert_no_permission(response)
    fee.refresh_from_db()
    assert fee.amount == Decimal("149.00")


@pytest.mark.django_db
def test_update_changes_the_charge(merchant_api_client, fee):
    response = merchant_api_client.post_graphql(
        UPDATE,
        {"id": fee_id(fee), "input": {"label": "Crating and strapping", "amount": 199}},
    )

    payload = get_graphql_content(response)["data"]["wsmFeeUpdate"]
    assert payload["errors"] == []
    fee.refresh_from_db()
    assert fee.label == "Crating and strapping"
    assert fee.amount == Decimal("199.00")


@pytest.mark.django_db
def test_the_update_input_will_not_take_the_carrier_variant(
    merchant_api_client, fee, variant
):
    """Read-only on purpose: `Fee.ensure_variant` writes it on the first buy.

    A merchant who could set it can only point a charge at the wrong catalog
    row, and nothing on any screen would show that they had.
    """
    response = merchant_api_client.post_graphql(
        UPDATE_NAMING_THE_VARIANT,
        {
            "id": fee_id(fee),
            "variant": graphene.Node.to_global_id("ProductVariant", variant.pk),
        },
    )

    content = response.json()
    assert "errors" in content, "the input accepted a field it must not carry"
    assert "variant" in content["errors"][0]["message"]
    fee.refresh_from_db()
    assert fee.variant_id is None


# --- wsmFeeDelete ------------------------------------------------------------


@pytest.mark.django_db
def test_delete_is_refused_without_the_permission(staff_api_client, fee):
    response = staff_api_client.post_graphql(DELETE, {"id": fee_id(fee)})

    assert_no_permission(response)
    assert Fee.objects.filter(pk=fee.pk).exists()


@pytest.mark.django_db
def test_delete_removes_the_charge(merchant_api_client, fee):
    response = merchant_api_client.post_graphql(DELETE, {"id": fee_id(fee)})

    payload = get_graphql_content(response)["data"]["wsmFeeDelete"]
    assert payload["errors"] == []
    assert not Fee.objects.exists()


# --- wsmFeeBulkDelete --------------------------------------------------------


@pytest.mark.django_db
def test_bulk_delete_is_refused_without_the_permission(staff_api_client, fee):
    response = staff_api_client.post_graphql(BULK_DELETE, {"ids": [fee_id(fee)]})

    assert_no_permission(response)
    assert Fee.objects.filter(pk=fee.pk).exists()


@pytest.mark.django_db
def test_bulk_delete_removes_only_the_rows_it_was_given(
    merchant_api_client, fee, product_list
):
    spared = Fee.objects.create(
        product=product_list[0],
        label="Hazmat handling",
        basis=pricing.FIXED,
        amount=Decimal("25.00"),
    )

    response = merchant_api_client.post_graphql(BULK_DELETE, {"ids": [fee_id(fee)]})

    payload = get_graphql_content(response)["data"]["wsmFeeBulkDelete"]
    assert payload["errors"] == []
    assert payload["count"] == 1
    assert list(Fee.objects.values_list("pk", flat=True)) == [spared.pk]
