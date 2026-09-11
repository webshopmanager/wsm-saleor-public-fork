# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The schema this fork serves: stock's, with our fields mixed in.

Rung 1 of Bill's integration ladder, not rung 5. `saleor/graphql/api.py` is
imported, never rebound: its `Query` and `Mutation` are SUBCLASSED here, and the
result goes through the same `build_federated_schema` call with the same
arguments. `saleor/wsm/urls.py` then serves this schema at `graphql/` and hands
the rest of the URL space to `saleor.urls`, so the whole attachment is one
settings line and zero monkey patches.

`API_SCHEMA_SOURCE` is the other half. Composing "with the same arguments" is an
assumption about a core file, and an upstream bump that adds a `types=` entry or
a directive would leave this schema quietly missing it: stock's own schema would
have the new type and the one we serve would not. The digest below is over
api.py's construction block itself, so that bump is a red test with a diff to
read rather than a field that stopped existing.

COST, measured and accepted: two full Saleor schemas are built at URL-conf load,
because `saleor.urls` builds its own on import and this module cannot stop it
without the patch this design exists to avoid. ponytail: the ceiling is boot
time and resident memory on a cold worker, nothing per-request (the stock
schema's `graphql/` route is never reached, ours matches first). The upgrade
path, if a cold start ever has to get cheaper, is a rung-5 rebind of
`saleor.graphql.api.schema` from `ready()` with a `PINNED` entry, which buys
back one build at the cost of one monkey patch.
"""

import hashlib
import inspect

import graphql

from ...graphql import api
from ...graphql.core.federation.schema import build_federated_schema
from .compose.schema import ComposeMutations, ComposeQueries
from .dealer.schema import WsmDealerMutations, WsmDealerQueries

_BLOCK_START = "schema = build_federated_schema("
_BLOCK_END = "monitor_fields_usage(schema)"

# sha256 of api.py's schema-construction block, as `api_schema_source` reads it.
# To re-pin after a deliberate upstream bump: read the diff for that block
# FIRST, mirror any new argument below, then print the new digest with
#   python -c "from saleor.wsm.graphql import schema; print(schema.source_digest())"
API_SCHEMA_SOURCE = "b95661439d71fec7882bda1e396bf37524624a38b5980ea902ac8d5509060418"


def api_schema_source(source: str | None = None) -> str:
    """The lines of `saleor/graphql/api.py` that build the schema, verbatim.

    `source` is an argument so a test can hand this function a MODIFIED api.py
    and prove the digest moves. A tripwire nobody has watched go off is a
    comment.
    """
    if source is None:
        source = inspect.getsource(api)
    start = source.index(_BLOCK_START)
    end = source.index(_BLOCK_END, start) + len(_BLOCK_END)
    return source[start:end]


def source_digest(source: str | None = None) -> str:
    return hashlib.sha256(api_schema_source(source).encode()).hexdigest()


class WsmQueries(ComposeQueries, WsmDealerQueries):
    """Every query field this fork adds. One mixin per domain."""


class WsmMutations(ComposeMutations, WsmDealerMutations):
    """Every mutation this fork adds. One mixin per domain."""


# `type()` rather than a `class` statement so the GraphQL type keeps stock's
# name. A schema whose root is called `WsmQuery` is not the schema the Dashboard
# and every existing client were written against.
Query = type("Query", (WsmQueries, api.Query), {})
Mutation = type("Mutation", (WsmMutations, api.Mutation), {})

# Identical to the call in api.py, by construction: every argument is read back
# off `api` rather than re-spelled, and the digest above is what says the call
# itself still looks like this.
schema = build_federated_schema(
    Query,
    mutation=Mutation,
    types=(
        api.unit_enums
        + list(api.WEBHOOK_TYPES_MAP.values())
        + api.PAYMENT_ADDITIONAL_TYPES
        + api.ASSIGNED_ATTRIBUTE_TYPES
    ),
    subscription=api.Subscription,
    directives=graphql.specified_directives
    + [api.GraphQLDocDirective, api.GraphQLWebhookEventsInfoDirective],
)
api.monitor_fields_usage(schema)
