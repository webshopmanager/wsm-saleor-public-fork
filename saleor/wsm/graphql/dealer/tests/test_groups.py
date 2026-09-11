# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Buyer groups over GraphQL, through the real view.

Every assertion goes through `post_graphql`, which under this branch hits the
composed schema mounted by `saleor/wsm/urls.py`. A resolver called by hand
cannot tell a field that exists from a field that is SERVED.

Two rules here are worth more than the CRUD around them:

- A group CODE is a name other tables point at by string (`tier_group` in
  wsm.compose is a bare CharField), so a duplicate code silently changes what a
  compose delta prices against. It is refused with its own code, not "unique".
- Reads are gated on MANAGE_DISCOUNTS *or* MANAGE_PRODUCTS, because the compose
  option-set screen offers a picker of these codes and is gated on the latter.
  Writes stay MANAGE_DISCOUNTS, and the test below is the one that says so.
"""

import graphene
import pytest

from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from ....dealer.models import DealerCustomer, DealerGroup, TierPrice
from ...utils import BULK_LIMIT

pytestmark = pytest.mark.django_db


CREATE = """
    mutation Create($input: WsmDealerGroupCreateInput!) {
      wsmDealerGroupCreate(input: $input) {
        dealerGroup { id code name tierPriceCount customerCount }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation Update($id: ID!, $input: WsmDealerGroupUpdateInput!) {
      wsmDealerGroupUpdate(id: $id, input: $input) {
        dealerGroup { id code name }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation Delete($id: ID!) {
      wsmDealerGroupDelete(id: $id) {
        dealerGroup { id code }
        errors { field code message }
      }
    }
"""

BULK_DELETE = """
    mutation BulkDelete($ids: [ID!]!) {
      wsmDealerGroupBulkDelete(ids: $ids) {
        count
        errors { field code message }
      }
    }
"""

DETAIL = """
    query Detail($id: ID, $code: String) {
      wsmDealerGroup(id: $id, code: $code) {
        id code name tierPriceCount customerCount
      }
    }
"""

PICKER = """
    query Picker {
      wsmDealerGroups(first: 100) {
        edges { node { id code name } }
      }
    }
"""

LIST = """
    query List($filter: WsmDealerGroupFilterInput) {
      wsmDealerGroups(filter: $filter, first: 20) {
        totalCount
        edges { node { id code name tierPriceCount customerCount } }
      }
    }
"""


def group_gid(group):
    return graphene.Node.to_global_id("WsmDealerGroup", group.pk)


@pytest.fixture
def dealer_group(db):
    return DealerGroup.objects.create(code="dealer-1", name="Dealer tier 1")


# --- permissions -------------------------------------------------------------


def test_an_anonymous_caller_cannot_read_a_group(api_client, dealer_group):
    response = api_client.post_graphql(DETAIL, {"id": group_gid(dealer_group)})

    assert_no_permission(response)


def test_staff_with_neither_permission_cannot_read_a_group(
    staff_api_client, dealer_group
):
    response = staff_api_client.post_graphql(DETAIL, {"id": group_gid(dealer_group)})

    assert_no_permission(response)


def test_manage_products_alone_can_read_a_group_for_the_compose_picker(
    staff_api_client, dealer_group, permission_manage_products
):
    """The amendment of 2026-09-10, as a test rather than a comment.

    The compose option-set screen lists these codes to price tier deltas
    against, and that screen is MANAGE_PRODUCTS. Without this, a merchant
    editing an option set gets an empty picker and types a code that prices
    nothing.
    """
    response = staff_api_client.post_graphql(
        LIST, {}, permissions=[permission_manage_products]
    )

    content = get_graphql_content(response)
    codes = [
        edge["node"]["code"] for edge in content["data"]["wsmDealerGroups"]["edges"]
    ]
    assert codes == ["dealer-1"]


def test_manage_products_alone_cannot_write_a_group(
    staff_api_client, permission_manage_products
):
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": {"code": "warehouse"}},
        permissions=[permission_manage_products],
    )

    assert_no_permission(response)
    assert not DealerGroup.objects.exists()


def test_the_create_is_refused_without_the_permission(staff_api_client):
    response = staff_api_client.post_graphql(CREATE, {"input": {"code": "warehouse"}})

    assert_no_permission(response)
    assert not DealerGroup.objects.exists()


def test_the_delete_is_refused_without_the_permission(staff_api_client, dealer_group):
    response = staff_api_client.post_graphql(DELETE, {"id": group_gid(dealer_group)})

    assert_no_permission(response)
    assert DealerGroup.objects.filter(pk=dealer_group.pk).exists()


# --- create ------------------------------------------------------------------


def test_creating_a_group_stores_the_code_and_the_name(
    staff_api_client, permission_manage_discounts
):
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": {"code": "warehouse", "name": "Warehouse pricing"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupCreate"]
    assert payload["errors"] == []
    assert payload["dealerGroup"]["code"] == "warehouse"
    assert payload["dealerGroup"]["name"] == "Warehouse pricing"
    assert payload["dealerGroup"]["tierPriceCount"] == 0
    assert payload["dealerGroup"]["customerCount"] == 0
    assert DealerGroup.objects.get(code="warehouse").name == "Warehouse pricing"


def test_a_second_group_cannot_take_a_code_that_is_already_in_use(
    staff_api_client, permission_manage_discounts, dealer_group
):
    """The code is what wsm.compose stores, so two rows for it price nothing."""
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": {"code": "dealer-1", "name": "A second one"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupCreate"]
    assert payload["dealerGroup"] is None
    assert payload["errors"] == [
        {
            "field": "code",
            "code": "DUPLICATE_GROUP_CODE",
            "message": "'dealer-1' already names a dealer group",
        }
    ]
    assert DealerGroup.objects.count() == 1


def test_a_group_needs_a_code(staff_api_client, permission_manage_discounts):
    response = staff_api_client.post_graphql(
        CREATE, {"input": {"code": "   "}}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupCreate"]
    assert payload["errors"][0]["code"] == "REQUIRED"
    assert not DealerGroup.objects.exists()


# --- update ------------------------------------------------------------------


def test_updating_a_group_renames_it(
    staff_api_client, permission_manage_discounts, dealer_group
):
    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": group_gid(dealer_group), "input": {"name": "Tier one"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupUpdate"]
    assert payload["errors"] == []
    assert payload["dealerGroup"]["name"] == "Tier one"
    dealer_group.refresh_from_db()
    assert dealer_group.name == "Tier one"
    assert dealer_group.code == "dealer-1"


def test_a_group_cannot_be_renamed_onto_another_groups_code(
    staff_api_client, permission_manage_discounts, dealer_group
):
    other = DealerGroup.objects.create(code="warehouse")

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": group_gid(other), "input": {"code": "dealer-1"}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupUpdate"]
    assert payload["errors"][0]["code"] == "DUPLICATE_GROUP_CODE"
    other.refresh_from_db()
    assert other.code == "warehouse"


def test_a_group_keeping_its_own_code_is_not_a_duplicate(
    staff_api_client, permission_manage_discounts, dealer_group
):
    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": group_gid(dealer_group),
            "input": {"code": "dealer-1", "name": "Same code, new name"},
        },
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerGroupUpdate"]["errors"] == []


# --- delete ------------------------------------------------------------------


def test_deleting_an_empty_group_removes_it(
    staff_api_client, permission_manage_discounts, dealer_group
):
    response = staff_api_client.post_graphql(
        DELETE,
        {"id": group_gid(dealer_group)},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupDelete"]
    assert payload["errors"] == []
    assert payload["dealerGroup"]["code"] == "dealer-1"
    assert not DealerGroup.objects.exists()


def test_a_group_with_customers_in_it_is_refused_not_a_server_error(
    staff_api_client, permission_manage_discounts, dealer_group, customer_user
):
    """`on_delete=PROTECT` would raise ProtectedError out of the view.

    The merchant's honest answer is a sentence naming how many shoppers are in
    the way, which is what this asserts. A 500 here is the defect.
    """
    DealerCustomer.objects.create(user=customer_user, group=dealer_group)

    response = staff_api_client.post_graphql(
        DELETE,
        {"id": group_gid(dealer_group)},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupDelete"]
    assert payload["errors"] == [
        {
            "field": "id",
            "code": "GROUP_IN_USE",
            "message": (
                "1 customer(s) buy at this group's prices. Move them to another "
                "group first."
            ),
        }
    ]
    assert DealerGroup.objects.filter(pk=dealer_group.pk).exists()


# --- bulk delete -------------------------------------------------------------


def test_bulk_delete_removes_every_empty_group(
    staff_api_client, permission_manage_discounts, dealer_group
):
    other = DealerGroup.objects.create(code="warehouse")

    response = staff_api_client.post_graphql(
        BULK_DELETE,
        {"ids": [group_gid(dealer_group), group_gid(other)]},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupBulkDelete"]
    assert payload["errors"] == []
    assert payload["count"] == 2
    assert not DealerGroup.objects.exists()


def test_bulk_delete_skips_a_group_that_still_has_customers(
    staff_api_client, permission_manage_discounts, dealer_group, customer_user
):
    other = DealerGroup.objects.create(code="warehouse")
    DealerCustomer.objects.create(user=customer_user, group=dealer_group)

    response = staff_api_client.post_graphql(
        BULK_DELETE,
        {"ids": [group_gid(dealer_group), group_gid(other)]},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerGroupBulkDelete"]
    assert payload["count"] == 1
    # Field AND code, not just the sentence. The Dashboard shows a message as a
    # form error only when `field` is empty and branches on the code for
    # everything else, and stock's bulk path hands it a base64 node id with no
    # code at all (`saleor/graphql/core/mutations.py:1066-1067`).
    assert payload["errors"] == [
        {
            "field": "ids.0",
            "code": "GROUP_IN_USE",
            "message": (
                "1 customer(s) buy at this group's prices. Move them to "
                "another group first."
            ),
        }
    ]
    assert list(DealerGroup.objects.values_list("code", flat=True)) == ["dealer-1"]


# --- reads -------------------------------------------------------------------


def test_a_group_is_found_by_the_code_other_tables_point_at(
    staff_api_client, permission_manage_discounts, dealer_group
):
    response = staff_api_client.post_graphql(
        DETAIL, {"code": "dealer-1"}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerGroup"]["id"] == group_gid(dealer_group)


def test_the_group_list_counts_its_prices_and_its_customers(
    staff_api_client,
    permission_manage_discounts,
    dealer_group,
    customer_user,
    variant,
):
    DealerCustomer.objects.create(user=customer_user, group=dealer_group)
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="10.00"
    )
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=10, amount="9.00"
    )

    response = staff_api_client.post_graphql(
        LIST, {}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    node = content["data"]["wsmDealerGroups"]["edges"][0]["node"]
    assert node["tierPriceCount"] == 2
    assert node["customerCount"] == 1


def test_the_group_list_search_matches_the_code_and_the_name(
    staff_api_client, permission_manage_discounts, dealer_group
):
    DealerGroup.objects.create(code="warehouse", name="Bulk buyers")

    response = staff_api_client.post_graphql(
        LIST, {"filter": {"search": "bulk"}}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    codes = [
        edge["node"]["code"] for edge in content["data"]["wsmDealerGroups"]["edges"]
    ]
    assert codes == ["warehouse"]


def test_a_group_read_pays_only_for_the_counts_it_was_asked_for(
    staff_api_client,
    permission_manage_discounts,
    dealer_group,
    customer_user,
    variant,
    capture_queries,
):
    """Two counts in one `annotate` cross-multiply, and nobody always wants them.

    The compose option-set picker and the single-group lookup select id, code
    and name, so a count annotated unconditionally is a join and an aggregate
    bought for a field nobody read. When they ARE selected, two multi-valued
    `Count(distinct=True)` annotations sharing one FROM clause make the database
    build the product of both joins before deduplicating, which on live data
    (304 tier rows, 40 customers) is 12,160 intermediate rows for two numbers.
    A scalar subquery per count joins nothing.
    """
    DealerCustomer.objects.create(user=customer_user, group=dealer_group)
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount="10.00"
    )
    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    groups = DealerGroup._meta.db_table
    counted = (TierPrice._meta.db_table, DealerCustomer._meta.db_table)

    with capture_queries() as picker:
        get_graphql_content(staff_api_client.post_graphql(PICKER))
    reads = [q["sql"] for q in picker.captured_queries if groups in q["sql"]]
    assert reads
    assert not [sql for sql in reads if any(table in sql for table in counted)], reads

    with capture_queries() as listed:
        content = get_graphql_content(staff_api_client.post_graphql(LIST))
    node = content["data"]["wsmDealerGroups"]["edges"][0]["node"]
    assert (node["tierPriceCount"], node["customerCount"]) == (1, 1)
    joins = [
        q["sql"]
        for q in listed.captured_queries
        if groups in q["sql"] and "JOIN" in q["sql"]
    ]
    assert not joins, joins


def test_bulk_delete_refuses_more_ids_than_the_cap(
    staff_api_client, permission_manage_discounts, dealer_group
):
    """Bounded before a single id is decoded, and nothing is deleted."""
    ids = [group_gid(dealer_group)] * (BULK_LIMIT + 1)

    response = staff_api_client.post_graphql(
        BULK_DELETE, {"ids": ids}, permissions=[permission_manage_discounts]
    )

    payload = get_graphql_content(response)["data"]["wsmDealerGroupBulkDelete"]
    assert [(e["field"], e["code"]) for e in payload["errors"]] == [
        ("ids", "BULK_LIMIT")
    ]
    assert payload["count"] == 0
    assert DealerGroup.objects.filter(pk=dealer_group.pk).exists()
