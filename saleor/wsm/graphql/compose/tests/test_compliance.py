# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The compliance card and the fleet-wide audit list, over the wire.

Prop 65 and shipping restrictions are fleet-wide baseline (Dana, 2026-09-09),
so this row exists on any product a merchant says it does. It is a OneToOne, so
there is no create mutation: `wsmProductComplianceUpdate` is keyed by the
PRODUCT and creates the row on first use, which is what the admin's
add-on-first-visit was standing in for.
"""

import graphene
import pytest

from saleor.graphql.tests.utils import assert_no_permission, get_graphql_content
from saleor.wsm.compose.models import ProductCompliance

DETAIL = """
    query WsmProductCompliance($product: ID, $id: ID) {
      wsmProductCompliance(product: $product, id: $id) {
        id
        prop65
        prop65Text
        restrictedStates
        restrictionMessage
        includeShippingZones { id name }
        product { id }
      }
    }
"""

LIST = """
    query WsmProductCompliances($filter: WsmProductComplianceFilterInput) {
      wsmProductCompliances(first: 20, filter: $filter) {
        totalCount
        edges { node { id prop65 } }
      }
    }
"""

UPDATE = """
    mutation WsmProductComplianceUpdate(
      $product: ID!, $input: WsmProductComplianceInput!
    ) {
      wsmProductComplianceUpdate(product: $product, input: $input) {
        compliance {
          id
          prop65
          restrictedStates
          includeShippingZones { id }
        }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation WsmProductComplianceDelete($id: ID!) {
      wsmProductComplianceDelete(id: $id) {
        compliance { id }
        errors { field code message }
      }
    }
"""


def product_id(product):
    return graphene.Node.to_global_id("Product", product.pk)


def row_id(row):
    return graphene.Node.to_global_id("WsmProductCompliance", row.pk)


# --- reads -------------------------------------------------------------------


@pytest.mark.django_db
def test_an_anonymous_caller_is_refused(api_client, compliance):
    response = api_client.post_graphql(
        DETAIL, {"product": product_id(compliance.product)}
    )

    assert_no_permission(response)


@pytest.mark.django_db
def test_the_card_reads_by_product_and_normalises_the_state_codes(
    merchant_api_client, compliance
):
    compliance.restricted_states = "ca, hi"
    compliance.save()

    response = merchant_api_client.post_graphql(
        DETAIL, {"product": product_id(compliance.product)}
    )

    data = get_graphql_content(response)["data"]["wsmProductCompliance"]
    assert data["prop65"] is True
    # The list the Dashboard renders is the list the checkout matcher compares
    # against, character for character: both come from `state_codes`.
    assert data["restrictedStates"] == ["CA", "HI"]


@pytest.mark.django_db
def test_the_card_is_null_for_a_product_with_no_row(merchant_api_client, product):
    response = merchant_api_client.post_graphql(
        DETAIL, {"product": product_id(product)}
    )

    assert get_graphql_content(response)["data"]["wsmProductCompliance"] is None


@pytest.mark.django_db
def test_asking_by_both_keys_at_once_is_said_to_be_a_caller_bug(
    merchant_api_client, compliance
):
    """Two questions in one call: answering whichever the code picked is worse."""
    response = merchant_api_client.post_graphql(
        DETAIL,
        {"product": product_id(compliance.product), "id": row_id(compliance)},
    )

    content = response.json()
    assert "errors" in content
    assert "exactly one" in content["errors"][0]["message"]


@pytest.mark.django_db
def test_the_audit_list_filters_on_prop65(
    merchant_api_client, compliance, product_list
):
    ProductCompliance.objects.create(product=product_list[0], prop65=False)

    response = merchant_api_client.post_graphql(LIST, {"filter": {"prop65": True}})

    data = get_graphql_content(response)["data"]["wsmProductCompliances"]
    assert data["totalCount"] == 1
    assert data["edges"][0]["node"]["id"] == row_id(compliance)


# --- wsmProductComplianceUpdate ----------------------------------------------


@pytest.mark.django_db
def test_update_is_refused_without_the_permission(staff_api_client, product):
    response = staff_api_client.post_graphql(
        UPDATE, {"product": product_id(product), "input": {"prop65": True}}
    )

    assert_no_permission(response)
    assert not ProductCompliance.objects.exists()


@pytest.mark.django_db
def test_update_creates_the_row_on_first_use_and_never_a_second(
    merchant_api_client, product
):
    """Create-or-update on a OneToOne: stock's `get_instance` would make two.

    Stock returns a fresh `model()` when no id is supplied, which is right for a
    create/update pair and wrong for a table keyed by its product: the second
    save would either duplicate the row or hit the unique index as a 500.
    """
    for prop65 in (True, False, True):
        response = merchant_api_client.post_graphql(
            UPDATE,
            {"product": product_id(product), "input": {"prop65": prop65}},
        )
        payload = get_graphql_content(response)["data"]["wsmProductComplianceUpdate"]
        assert payload["errors"] == []

    assert ProductCompliance.objects.count() == 1
    assert ProductCompliance.objects.get().prop65 is True


@pytest.mark.django_db
def test_update_stores_the_states_as_the_model_reads_them(merchant_api_client, product):
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "product": product_id(product),
            "input": {"restrictedStates": ["ca", "hi"], "prop65": True},
        },
    )

    payload = get_graphql_content(response)["data"]["wsmProductComplianceUpdate"]
    assert payload["errors"] == []
    assert payload["compliance"]["restrictedStates"] == ["CA", "HI"]
    assert ProductCompliance.objects.get().restricted_states == "CA, HI"


@pytest.mark.django_db
def test_update_refuses_a_code_that_is_not_a_state(merchant_api_client, product):
    """CAL for California restricts nothing, and no order would ever say so."""
    response = merchant_api_client.post_graphql(
        UPDATE,
        {"product": product_id(product), "input": {"restrictedStates": ["CAL"]}},
    )

    payload = get_graphql_content(response)["data"]["wsmProductComplianceUpdate"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("restrictedStates", "UNKNOWN_US_STATE_CODE")
    ]
    assert not ProductCompliance.objects.exists()


@pytest.mark.django_db
def test_update_replaces_the_whole_zone_set(
    merchant_api_client, compliance, shipping_zone
):
    zone_id = graphene.Node.to_global_id("ShippingZone", shipping_zone.pk)

    added = merchant_api_client.post_graphql(
        UPDATE,
        {
            "product": product_id(compliance.product),
            "input": {"includeShippingZones": [zone_id]},
        },
    )
    payload = get_graphql_content(added)["data"]["wsmProductComplianceUpdate"]
    assert payload["errors"] == []
    assert [zone["id"] for zone in payload["compliance"]["includeShippingZones"]] == [
        zone_id
    ]

    cleared = merchant_api_client.post_graphql(
        UPDATE,
        {
            "product": product_id(compliance.product),
            "input": {"includeShippingZones": []},
        },
    )
    payload = get_graphql_content(cleared)["data"]["wsmProductComplianceUpdate"]
    assert payload["errors"] == []
    assert payload["compliance"]["includeShippingZones"] == []
    assert not compliance.include_shipping_zones.exists()


# --- wsmProductComplianceDelete ----------------------------------------------


@pytest.mark.django_db
def test_delete_is_refused_without_the_permission(staff_api_client, compliance):
    response = staff_api_client.post_graphql(DELETE, {"id": row_id(compliance)})

    assert_no_permission(response)
    assert ProductCompliance.objects.filter(pk=compliance.pk).exists()


@pytest.mark.django_db
def test_delete_removes_the_row(merchant_api_client, compliance):
    response = merchant_api_client.post_graphql(DELETE, {"id": row_id(compliance)})

    payload = get_graphql_content(response)["data"]["wsmProductComplianceDelete"]
    assert payload["errors"] == []
    assert not ProductCompliance.objects.exists()
