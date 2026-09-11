# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every WSM list costs what its stock sibling costs, per page.

Saleor charges a query before it runs it (`saleor/graphql/query_cost_map.py`,
`GRAPHQL_QUERY_MAX_COMPLEXITY`), and a field the map does not mention is free.
The whole WSM surface was free: `products(first: 100)` cost 200 and
`wsmDealerGroups(first: 100)` cost 0, so a caller who could not ask stock for a
hundred products could ask us for five hundred tier prices. The guard is only a
guard if it can see the fields we added.

Two tests, because two things can go wrong. The first is the one a screen
notices: a list costs per page. The second is the one nobody notices: a field
added later with no entry, which is free again and silent. That one is asserted
over the SERVED schema rather than over a list somebody has to remember to
extend.
"""

import pytest

from ....graphql.query_cost_map import COST_MAP

pytestmark = pytest.mark.django_db


LIST = """
    query Groups($first: Int!) {
      wsmDealerGroups(first: $first) {
        edges { node { id code } }
      }
    }
"""


def _cost(client, first):
    response = client.post_graphql(LIST, {"first": first})
    return response.json()["extensions"]["cost"]["requestedQueryCost"]


def test_a_wsm_list_costs_per_page_the_way_a_stock_list_does(
    staff_api_client, permission_manage_discounts
):
    staff_api_client.user.user_permissions.add(permission_manage_discounts)

    ten = _cost(staff_api_client, 10)
    hundred = _cost(staff_api_client, 100)

    assert ten > 0, "wsmDealerGroups is invisible to the query cost guard"
    assert hundred == ten * 10, (
        f"{hundred} for 100 rows and {ten} for 10: the cost is not per page"
    )


def test_every_wsm_root_field_carries_a_cost_hint():
    """The class fix, asserted over the schema rather than over a list.

    A `Wsm*` field added to `Query` without an entry in the cost map is free,
    and free is the bug this wave fixed. Read off the served schema, so a new
    field is a red test in the run that adds it.
    """
    from ..schema import schema

    served = schema.get_query_type().fields
    wsm_fields = {name for name in served if name.startswith("wsm")}
    assert wsm_fields, "no wsm root field was found, so this test proved nothing"

    priced = set(COST_MAP.get("Query", {}))
    assert wsm_fields <= priced, (
        f"no query cost hint for {sorted(wsm_fields - priced)}: those fields are "
        "free to the complexity guard. Add them to saleor/wsm/graphql/cost.py."
    )


def test_every_priced_wsm_field_is_a_field_the_schema_serves():
    """The other direction: a typo in the map is a GraphQLError on every request.

    `validate_cost_map` walks the whole map against the schema on every query,
    so an entry naming a field this branch does not serve breaks EVERY request,
    not just one that selects it.
    """
    from ..schema import schema

    type_map = schema.get_type_map()
    for type_name, fields in COST_MAP.items():
        assert type_name in type_map, type_name
        for field in fields:
            assert field in type_map[type_name].fields, f"{type_name}.{field}"
