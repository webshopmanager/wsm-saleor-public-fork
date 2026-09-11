# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What a WSM list costs the complexity guard, and which schema it weighs.

Saleor charges a query BEFORE it runs it: `saleor/graphql/views.py:479` hands a
cost map and a schema to the validator on every request, and
`GRAPHQL_QUERY_MAX_COMPLEXITY` is the ceiling. Measured on the composed schema,
2026-09-11: `products(first: 100)` cost 200 and `wsmDealerGroups(first: 100)`
cost 0. So did every other `Wsm*` connection. The whole layer was free, and free
is not a smaller number: it is a surface with no ceiling on it at all.

TWO things were wrong, and the second is why the first alone is decoration.

1. No cost entry. Stock's `COST_MAP` never mentioned a `wsm*` field, and a field
   the map does not name costs the default, which is zero.

2. The guard was weighing the wrong schema. `views.py:38` imports
   `from .api import API_PATH, schema`, the STOCK schema object, and line 479
   validates against that module global while line 358 PARSES the document with
   `self.schema`, the one the view was constructed with. On stock Saleor the two
   are the same object and nobody notices. On this fork they are not: the view
   `saleor/wsm/urls.py` mounts serves the COMPOSED schema, so the cost walk ran
   over a schema in which no `Wsm*` field exists, found no type for them, and
   charged nothing no matter what the map said. Adding entries without fixing
   this makes `validate_cost_map` reject them ("cost map contains a field
   wsmOptionSet not defined by the Query type") and breaks EVERY request, which
   is how it was caught.

`install()` does both halves, and it is called from
`saleor/wsm/graphql/schema.py` the moment the composed schema exists, so the two
can never be out of step. The rebind is ONE module attribute
(`saleor.graphql.views.schema`), it is ledgered as MP5 in
`docs/wsm/CORE-TOUCHES.md`, and it is pinned in `saleor/wsm/patches.py`
`REBOUND` with the same discover-and-compare tripwire the other patches have.
The composed schema is a superset of the stock one, so every stock query is
weighed exactly as it was.

The entries mirror stock's own connections exactly (`complexity: 1`, multiplied
by `first` and `last`), so a WSM list costs what its stock sibling costs and one
ceiling covers both.

ponytail: three literal dicts, one per domain, rather than a registry each
domain schema appends to. The ceiling is that a new query field needs a line
here as well as in its own schema; the tripwire under it is
`saleor/wsm/graphql/tests/test_query_cost.py`, which reads the SERVED schema and
reddens when a `wsm*` root field has no entry. A branch that does not carry a
domain drops that domain's dict with it: this branch serves no containers
GraphQL, so there is no `CONTAINERS_COST` here.
"""

from ...graphql.query_cost_map import COST_MAP

# Stock's shape for a connection, named once: cost one per row asked for.
PER_PAGE = {"complexity": 1, "multipliers": ["first", "last"]}
# ... and for a single object, which is one row however it is looked up.
PER_OBJECT = {"complexity": 1}

COMPOSE_COST = {
    "Query": {
        "wsmOptionSet": PER_OBJECT,
        "wsmOptionSets": PER_PAGE,
        "wsmFee": PER_OBJECT,
        "wsmFees": PER_PAGE,
        "wsmProductCompliance": PER_OBJECT,
        "wsmProductCompliances": PER_PAGE,
    },
    # The three fields this layer appends to stock's `Product`
    # (`compose/product_extension.py`). Unpaged lists on a type that IS paged,
    # so they multiply recursively the way stock's own nested fields do.
    "Product": {
        "wsmOptionSets": PER_OBJECT,
        "wsmFees": PER_OBJECT,
        "wsmCompliance": PER_OBJECT,
    },
}

DEALER_COST = {
    "Query": {
        "wsmDealerGroup": PER_OBJECT,
        "wsmDealerGroups": PER_PAGE,
        "wsmDealerCustomer": PER_OBJECT,
        "wsmDealerCustomers": PER_PAGE,
        "wsmTierPrice": PER_OBJECT,
        "wsmTierPrices": PER_PAGE,
        # One row or none, and nothing to page through.
        "wsmDealerSettings": PER_OBJECT,
    },
}


def install(schema) -> None:
    """Price every WSM field, and point the guard at the schema we serve.

    Idempotent, and ORDERED: the rebind happens last, so a half-merged map can
    never be validated against a schema that would reject it.
    """
    for domain in (COMPOSE_COST, DEALER_COST):
        for type_name, fields in domain.items():
            COST_MAP.setdefault(type_name, {}).update(fields)

    # Imported here rather than at module scope: this module is about a map, and
    # `saleor.graphql.views` drags in the whole view stack. See the docstring
    # for why one attribute is rebound and where it is ledgered.
    from ...graphql import views

    # The marker `saleor/wsm/patches.py rebindings_installed()` discovers this
    # by, so the pin is compared against what is really bound rather than
    # against this line.
    schema._wsm_owned = True
    views.schema = schema
