# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Who is in a buyer group, over GraphQL, through the real view.

The row is a LINK, never the customer: unassigning takes the shopper out of the
group and leaves the account alone, which is the assertion that keeps a
merchant's "remove from dealer pricing" from deleting a customer.

A shopper buys at one group's prices or none (`DealerCustomer.user` is a
OneToOne), so a second assignment is a merchant looking at a stale list. It is
refused with its own code and a sentence naming the group they are already in,
because "unique" on that screen names nothing.
"""

import graphene
import pytest

from .....account.models import User
from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from ....dealer.models import DealerCustomer, DealerGroup

pytestmark = pytest.mark.django_db


ASSIGN = """
    mutation Assign($input: WsmDealerCustomerAssignInput!) {
      wsmDealerCustomerAssign(input: $input) {
        dealerCustomer {
          id
          taxExempt
          user { id email }
          group { id code }
        }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation Update($id: ID!, $input: WsmDealerCustomerUpdateInput!) {
      wsmDealerCustomerUpdate(id: $id, input: $input) {
        dealerCustomer { id taxExempt group { code } }
        errors { field code message }
      }
    }
"""

UNASSIGN = """
    mutation Unassign($id: ID!) {
      wsmDealerCustomerUnassign(id: $id) {
        dealerCustomer { id user { email } }
        errors { field code message }
      }
    }
"""

DETAIL = """
    query Detail($id: ID, $user: ID) {
      wsmDealerCustomer(id: $id, user: $user) {
        id taxExempt user { email } group { code }
      }
    }
"""

LIST = """
    query List($filter: WsmDealerCustomerFilterInput) {
      wsmDealerCustomers(filter: $filter, first: 20) {
        totalCount
        edges { node { id taxExempt user { email } group { code } } }
      }
    }
"""


def group_gid(group):
    return graphene.Node.to_global_id("WsmDealerGroup", group.pk)


def user_gid(user):
    return graphene.Node.to_global_id("User", user.pk)


def customer_gid(row):
    return graphene.Node.to_global_id("WsmDealerCustomer", row.pk)


@pytest.fixture
def dealer_group(db):
    return DealerGroup.objects.create(code="dealer-1", name="Dealer tier 1")


# --- permissions -------------------------------------------------------------


def test_an_anonymous_caller_cannot_read_an_assignment(api_client, customer_user):
    response = api_client.post_graphql(DETAIL, {"user": user_gid(customer_user)})

    assert_no_permission(response)


def test_manage_products_alone_cannot_read_an_assignment(
    staff_api_client, customer_user, permission_manage_products
):
    """The compose picker needs group CODES, never who is in them."""
    response = staff_api_client.post_graphql(
        DETAIL,
        {"user": user_gid(customer_user)},
        permissions=[permission_manage_products],
    )

    assert_no_permission(response)


def test_the_assign_is_refused_without_the_permission(
    staff_api_client, customer_user, dealer_group
):
    response = staff_api_client.post_graphql(
        ASSIGN,
        {"input": {"user": user_gid(customer_user), "group": group_gid(dealer_group)}},
    )

    assert_no_permission(response)
    assert not DealerCustomer.objects.exists()


def test_the_unassign_is_refused_without_the_permission(
    staff_api_client, customer_user, dealer_group
):
    row = DealerCustomer.objects.create(user=customer_user, group=dealer_group)

    response = staff_api_client.post_graphql(UNASSIGN, {"id": customer_gid(row)})

    assert_no_permission(response)
    assert DealerCustomer.objects.filter(pk=row.pk).exists()


# --- assign ------------------------------------------------------------------


def test_assigning_a_shopper_puts_them_in_the_group(
    staff_api_client, permission_manage_discounts, customer_user, dealer_group
):
    response = staff_api_client.post_graphql(
        ASSIGN,
        {
            "input": {
                "user": user_gid(customer_user),
                "group": group_gid(dealer_group),
                "taxExempt": True,
            }
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerCustomerAssign"]
    assert payload["errors"] == []
    assert payload["dealerCustomer"]["user"]["email"] == customer_user.email
    assert payload["dealerCustomer"]["group"]["code"] == "dealer-1"
    assert payload["dealerCustomer"]["taxExempt"] is True
    row = DealerCustomer.objects.get()
    assert row.user_id == customer_user.pk
    assert row.tax_exempt is True


def test_a_shopper_cannot_be_put_in_a_second_group(
    staff_api_client, permission_manage_discounts, customer_user, dealer_group
):
    DealerCustomer.objects.create(user=customer_user, group=dealer_group)
    other = DealerGroup.objects.create(code="warehouse")

    response = staff_api_client.post_graphql(
        ASSIGN,
        {"input": {"user": user_gid(customer_user), "group": group_gid(other)}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerCustomerAssign"]
    assert payload["dealerCustomer"] is None
    assert payload["errors"][0]["field"] == "user"
    assert payload["errors"][0]["code"] == "CUSTOMER_ALREADY_ASSIGNED"
    assert "dealer-1" in payload["errors"][0]["message"]
    assert DealerCustomer.objects.get().group_id == dealer_group.pk


def test_a_group_id_that_names_something_else_is_a_field_error(
    staff_api_client, permission_manage_discounts, customer_user, collection
):
    """A wrong-type global id is a merchant's stale tab, not a 500.

    The inherited `clean_input` resolves a bare `ID` with no expected type, so
    without `typed_ids` a Collection posted as `group` comes back as a
    Collection and the assignment that follows is a server error.
    """
    response = staff_api_client.post_graphql(
        ASSIGN,
        {
            "input": {
                "user": user_gid(customer_user),
                "group": graphene.Node.to_global_id("Collection", collection.pk),
            }
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerCustomerAssign"]
    assert payload["dealerCustomer"] is None
    assert payload["errors"][0]["field"] == "group"
    assert not DealerCustomer.objects.exists()


# --- update ------------------------------------------------------------------


def test_updating_moves_a_shopper_to_another_group(
    staff_api_client, permission_manage_discounts, customer_user, dealer_group
):
    row = DealerCustomer.objects.create(user=customer_user, group=dealer_group)
    other = DealerGroup.objects.create(code="warehouse")

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": customer_gid(row),
            "input": {"group": group_gid(other), "taxExempt": True},
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerCustomerUpdate"]
    assert payload["errors"] == []
    assert payload["dealerCustomer"]["group"]["code"] == "warehouse"
    row.refresh_from_db()
    assert row.group_id == other.pk
    assert row.tax_exempt is True
    assert DealerCustomer.objects.count() == 1


# --- unassign ----------------------------------------------------------------


def test_unassigning_removes_the_link_and_leaves_the_account(
    staff_api_client, permission_manage_discounts, customer_user, dealer_group
):
    row = DealerCustomer.objects.create(user=customer_user, group=dealer_group)

    response = staff_api_client.post_graphql(
        UNASSIGN,
        {"id": customer_gid(row)},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerCustomerUnassign"]
    assert payload["errors"] == []
    assert payload["dealerCustomer"]["user"]["email"] == customer_user.email
    assert not DealerCustomer.objects.exists()
    assert User.objects.filter(pk=customer_user.pk).exists()
    assert DealerGroup.objects.filter(pk=dealer_group.pk).exists()


# --- reads -------------------------------------------------------------------


def test_an_assignment_is_found_by_the_user_the_customer_card_is_open_on(
    staff_api_client, permission_manage_discounts, customer_user, dealer_group
):
    row = DealerCustomer.objects.create(user=customer_user, group=dealer_group)

    response = staff_api_client.post_graphql(
        DETAIL,
        {"user": user_gid(customer_user)},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerCustomer"]["id"] == customer_gid(row)


@pytest.fixture
def two_assigned_shoppers(customer_user, customer_user2, dealer_group):
    """One shopper in each of two groups, one of them tax exempt."""
    other = DealerGroup.objects.create(code="warehouse")
    DealerCustomer.objects.create(
        user=customer_user, group=dealer_group, tax_exempt=True
    )
    DealerCustomer.objects.create(user=customer_user2, group=other)
    return dealer_group, other


def emails_in(content):
    return [
        edge["node"]["user"]["email"]
        for edge in content["data"]["wsmDealerCustomers"]["edges"]
    ]


def test_the_customer_list_filters_by_group(
    staff_api_client, permission_manage_discounts, customer_user, two_assigned_shoppers
):
    mine, _other = two_assigned_shoppers

    response = staff_api_client.post_graphql(
        LIST,
        {"filter": {"group": group_gid(mine)}},
        permissions=[permission_manage_discounts],
    )

    assert emails_in(get_graphql_content(response)) == [customer_user.email]


def test_the_customer_list_filters_by_tax_status(
    staff_api_client, permission_manage_discounts, customer_user2, two_assigned_shoppers
):
    response = staff_api_client.post_graphql(
        LIST,
        {"filter": {"taxExempt": False}},
        permissions=[permission_manage_discounts],
    )

    assert emails_in(get_graphql_content(response)) == [customer_user2.email]
