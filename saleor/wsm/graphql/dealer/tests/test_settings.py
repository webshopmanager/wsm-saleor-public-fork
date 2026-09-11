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
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

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


# --- wave 2A: one row, and every reader ordered ------------------------------


def test_the_table_holds_one_settings_row_and_refuses_a_second(db):
    """The backstop under a toggle that decides what a shopper is charged.

    `wsmDealerSettingsUpdate` read `.first() or DealerSettings()`, so two
    concurrent saves of one screen each found no row and each INSERTed one.
    `stacking_enabled()` then read an UNORDERED `.first()`, so which of the two
    rows decided whether a voucher stacks on a dealer price was whichever
    Postgres handed back. One row at a fixed key makes the race unwritable.
    """
    DealerSettings.objects.create(discount_stacking=False)
    assert DealerSettings.objects.get().pk == 1

    # `bulk_create` skips `save()`, which is exactly the writer the constraint
    # is under: a 5.0 importer, a shell, a SQL fixup.
    with pytest.raises(IntegrityError), transaction.atomic():
        DealerSettings.objects.bulk_create([DealerSettings(pk=2)])


def test_the_stacking_reader_orders_the_row_it_takes(db):
    """A `.first()` with no `order_by` is 'whichever row the database returned'."""
    DealerSettings.objects.create(pk=1, discount_stacking=True)

    with CaptureQueriesContext(connection) as queries:
        assert DealerSettings.stacking_enabled() is True

    sql = " ".join(query["sql"] for query in queries.captured_queries)
    assert "ORDER BY" in sql.upper(), (
        "stacking_enabled() reads an unordered .first(): with two rows the "
        "toggle that decides whether a voucher stacks is row order"
    )


def test_the_update_writes_the_row_at_the_fixed_key(
    staff_api_client, permission_manage_discounts
):
    """The writer cannot make a second row, because it names the key it writes."""
    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    for stacking in (True, False, True):
        content = get_graphql_content(
            staff_api_client.post_graphql(
                UPDATE, {"input": {"discountStacking": stacking}}
            )
        )
        assert content["data"]["wsmDealerSettingsUpdate"]["errors"] == []

    assert list(DealerSettings.objects.values_list("pk", flat=True)) == [1]
    assert DealerSettings.stacking_enabled() is True
