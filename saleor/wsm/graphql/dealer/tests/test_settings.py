# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one settings detail the spine's own test does not cover.

`id` is null until the row is written.

Everything else about `wsmDealerSettings` and `wsmDealerSettingsUpdate` (the
permission, the defaults with no row, and the rule that a second call never
gives this table a second row) is asserted in
`saleor/wsm/graphql/tests/test_dealer_settings.py`, where the spine put it, and
is not repeated here.

The field exists because the Dashboard's settings screen has to tell "the
merchant has never touched this" from "the merchant turned it off", and a
default-shaped object with a null id is how the contract says so.
"""

import pytest

from .....graphql.tests.utils import get_graphql_content
from ....dealer.models import DealerSettings

pytestmark = pytest.mark.django_db


QUERY = """
    query { wsmDealerSettings { id discountStacking } }
"""

UPDATE = """
    mutation Update($input: WsmDealerSettingsInput!) {
      wsmDealerSettingsUpdate(input: $input) {
        dealerSettings { id discountStacking }
        errors { field code message }
      }
    }
"""


def test_the_settings_id_is_null_while_no_row_has_ever_been_written(
    staff_api_client, permission_manage_discounts
):
    assert not DealerSettings.objects.exists()

    response = staff_api_client.post_graphql(
        QUERY, {}, permissions=[permission_manage_discounts]
    )

    content = get_graphql_content(response)
    assert content["data"]["wsmDealerSettings"] == {
        "id": None,
        "discountStacking": False,
    }


def test_the_first_write_gives_the_settings_an_id(
    staff_api_client, permission_manage_discounts
):
    response = staff_api_client.post_graphql(
        UPDATE,
        {"input": {"discountStacking": True}},
        permissions=[permission_manage_discounts],
    )

    content = get_graphql_content(response)
    payload = content["data"]["wsmDealerSettingsUpdate"]
    assert payload["errors"] == []
    assert payload["dealerSettings"]["id"] is not None
    assert payload["dealerSettings"]["discountStacking"] is True
    assert DealerSettings.objects.get().discount_stacking is True
