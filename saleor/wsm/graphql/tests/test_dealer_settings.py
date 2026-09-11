# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One domain field, end to end, through the real GraphQL view.

Not a resolver called by hand: every assertion here goes through
`reverse("api")`, which under this branch is the composed schema mounted by
`saleor/wsm/urls.py`. That is the only way these tests can tell the difference
between a field that exists and a field that is SERVED.
"""

import pytest

from ....graphql.tests.utils import assert_no_permission, get_graphql_content
from ...dealer.models import DealerSettings

QUERY = """
    query {
      wsmDealerSettings {
        discountStacking
      }
    }
"""

MUTATION = """
    mutation WsmDealerSettingsUpdate($stacking: Boolean!) {
      wsmDealerSettingsUpdate(input: {discountStacking: $stacking}) {
        dealerSettings {
          discountStacking
        }
        errors {
          field
          code
          message
        }
      }
    }
"""


@pytest.mark.django_db
def test_an_anonymous_caller_is_refused(api_client):
    """A6's checkable clause: no staff token, no answer."""
    response = api_client.post_graphql(QUERY)

    assert_no_permission(response)


@pytest.mark.django_db
def test_a_staff_token_without_the_permission_is_refused(staff_api_client):
    """Staff is not enough. The Dashboard's permission groups govern this."""
    response = staff_api_client.post_graphql(QUERY)

    assert_no_permission(response)


@pytest.mark.django_db
def test_the_query_returns_the_row(staff_api_client, permission_manage_discounts):
    DealerSettings.objects.create(discount_stacking=True)

    response = staff_api_client.post_graphql(
        QUERY, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerSettings"]["discountStacking"] is True


@pytest.mark.django_db
def test_the_query_answers_with_the_defaults_when_no_row_exists(
    staff_api_client, permission_manage_discounts
):
    """No row means default-deny, and a merchant screen has to be able to SEE that."""
    assert not DealerSettings.objects.exists()

    response = staff_api_client.post_graphql(
        QUERY, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerSettings"]["discountStacking"] is False
    assert not DealerSettings.objects.exists(), "a read must not write a row"


@pytest.mark.django_db
def test_the_mutation_is_refused_without_the_permission(staff_api_client):
    DealerSettings.objects.create(discount_stacking=False)

    response = staff_api_client.post_graphql(MUTATION, {"stacking": True})

    assert_no_permission(response)
    assert DealerSettings.objects.get().discount_stacking is False


@pytest.mark.django_db
def test_the_mutation_updates_the_row(staff_api_client, permission_manage_discounts):
    row = DealerSettings.objects.create(discount_stacking=False)

    response = staff_api_client.post_graphql(
        MUTATION, {"stacking": True}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerSettingsUpdate"]
    assert payload["errors"] == []
    assert payload["dealerSettings"]["discountStacking"] is True
    row.refresh_from_db()
    assert row.discount_stacking is True


@pytest.mark.django_db
def test_the_mutation_creates_the_singleton_and_never_a_second_row(
    staff_api_client, permission_manage_discounts
):
    """The bug the inherited `get_instance` would have shipped.

    Stock returns a fresh `model()` when no id is supplied, which is right for a
    create-or-update pair and wrong for a table that holds one row: two calls
    would leave two rows and `stacking_enabled()` would read whichever came
    first.
    """
    # Granted once, not per call: `post_graphql(permissions=...)` asserts the
    # call is refused BEFORE it grants, which can only be true the first time.
    staff_api_client.user.user_permissions.add(permission_manage_discounts)

    for stacking in (True, False, True):
        response = staff_api_client.post_graphql(MUTATION, {"stacking": stacking})
        assert (
            get_graphql_content(response)["data"]["wsmDealerSettingsUpdate"]["errors"]
            == []
        )

    assert DealerSettings.objects.count() == 1
    assert DealerSettings.objects.get().discount_stacking is True
    assert DealerSettings.stacking_enabled() is True
