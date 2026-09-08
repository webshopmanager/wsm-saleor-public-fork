# WSM-FORK: fork-owned file. See FORK-NOTES.md.
import logging
import os
import re
import time
from contextlib import contextmanager

from django.db import DatabaseError, ProgrammingError, connections, models

logger = logging.getLogger(__name__)

SECONDARY_CATEGORIES_TABLE = "product_secondary_categories"

# Per-box kill switch, read once at import. Default ON; only an explicit
# off/false/0 (any case) disables, so an operator can hard-off a tenant without
# touching the table or waiting on a probe TTL. The other emergency lever,
# REVOKE SELECT on the table, works within one TTL and needs no deploy; this one
# is instantaneous and needs no database access at all. See FORK-NOTES.md.
_DISABLE_VALUES = {"off", "false", "0"}
_ENABLE_VALUES = {"", "on", "true", "1"}


def secondary_categories_env_enabled() -> bool:
    """Whether `WSM_SECONDARY_CATEGORIES` allows the feature on this box.

    Reads the environment live so it is testable with `monkeypatch.setenv`; the
    module caches the result in `SECONDARY_CATEGORIES_ENABLED` so the hot path
    pays one read at import, not one per request.

    A value that is neither a known ON nor a known OFF spelling (`no`, `disable`,
    a quote-wrapped `"false"`) is an operator error, and an operator setting this
    variable at all is trying to turn the feature OFF. So unknown values fail
    CLOSED and log, instead of silently leaving the feature on.
    """
    raw = os.environ.get("WSM_SECONDARY_CATEGORIES", "")
    value = raw.strip().strip("\"'").lower()
    if value in _ENABLE_VALUES:
        return True
    if value not in _DISABLE_VALUES:
        logger.warning(
            "WSM_SECONDARY_CATEGORIES=%r is not a recognised value; treating it "
            "as off. Use on/true/1 or off/false/0.",
            raw,
        )
    return False


SECONDARY_CATEGORIES_ENABLED = secondary_categories_env_enabled()

# SQLSTATEs that mean "this database cannot serve the secondary table right now",
# as opposed to "something else in the query is broken". Only these justify
# answering a request without the secondary leg; anything else propagates.
#   42P01 undefined_table         the table was dropped or renamed
#   42501 insufficient_privilege  SELECT was revoked from the runtime role
#   3F000 invalid_schema_name     the schema was swapped, or search_path moved
#   42703 undefined_column        a column was dropped or renamed
#   42883 undefined_function      a column's TYPE changed, so `bigint = <newtype>`
#                                 no longer resolves an operator; raised at plan
#                                 time on the secondary leg's `= ANY (ARRAY(...))`
# A column TYPE change is the "rebuilt with a different shape" event 42703 used to
# claim; Postgres reports it as 42883, not 42703 (42703 is a dropped or renamed
# column). Without it a type-changed rebuild 500s every category page
# indefinitely, because the probe never checks types so it never degrades.
# 42804 (datatype_mismatch) is deliberately NOT here: measured across 19 column
# types on both psc columns, no rebuild ever raises it; what does raise it is a
# UNION operand mismatch, and the guarded page statement is the fork's only UNION
# while its corroborator carries none, so accepting 42804 would let a broken
# union degrade to a wrong page at HTTP 200 with the probe flipped off.
TABLE_UNAVAILABLE_SQLSTATES = frozenset({"42P01", "42501", "3F000", "42703", "42883"})

# Of those, the two whose message is guaranteed to name the relation it is talking
# about ("relation X does not exist", "permission denied for table X"). For these
# the name is additional evidence and is required, so a missing-relation error
# about some OTHER table cannot be read as a verdict on ours. 42703 names a column,
# 3F000 names a schema, and 42883/42804 name an operator or a type rather than the
# relation, so none of those can be checked this way; they rely on the structural
# and corroboration layers instead.
SQLSTATES_NAMING_THE_RELATION = frozenset({"42P01", "42501"})

# How each of those states spells the relation it is complaining about, anchored so
# the name is captured rather than merely found somewhere in the text.
RELATION_IN_MESSAGE = {
    "42P01": re.compile(r'relation "(?P<name>[^"]+)" does not exist'),
    "42501": re.compile(r"permission denied for (?:table|relation|view) (?P<name>\S+)"),
}

# How long a probe result is trusted before it is re-checked. The table is
# created, granted and loaded by another service (PartsLogic), so its
# availability is not immutable and must not be cached for the process
# lifetime: a tenant that gains its first row has to start being served
# correctly without a Saleor restart, and a table that is dropped, renamed or
# has its grant revoked while Saleor is up has to stop being named in queries.
# Steady-state cost is one catalog lookup per interval per connection alias,
# against a page query it guards that issues three statements.
PROBE_TTL_SECONDS = 60

# alias -> (available, monotonic deadline)
_probe_cache: dict[str, tuple[bool, float]] = {}


@contextmanager
def recoverable_failure(database_connection_name: str):
    """Leave the connection usable if a statement inside the block fails.

    Do not take the savepoint unconditionally: reads run in autocommit, where
    there is no block to abort and nothing to protect, so the hot path must pay
    zero extra statements. Do not widen the `allow_writer()` blocks either: only
    the savepoint bookkeeping is wrapped, never the caller's statements, so the
    writer guard keeps applying to everything it exists to guard. See
    FORK-NOTES.md.
    """
    from ..core.db.connection import allow_writer

    connection = connections[database_connection_name]
    if not connection.in_atomic_block:
        yield
        return
    with allow_writer():
        savepoint = connection.savepoint()
    if savepoint is None:
        # Savepoints unavailable or suppressed. Nothing to protect with.
        yield
        return
    try:
        yield
    except Exception:
        with allow_writer():
            connection.savepoint_rollback(savepoint)
        raise
    else:
        with allow_writer():
            connection.savepoint_commit(savepoint)


def is_table_unavailable(error: ProgrammingError) -> bool:
    """Whether this error says the secondary table cannot be read right now.

    Evidence, not assumption. Django re-raises the driver's exception as its own
    with the original chained, so the SQLSTATE is on `__cause__`. A synthetic
    `ProgrammingError` with no SQLSTATE is deliberately NOT ours: without the
    driver's own verdict there is nothing to justify degrading a request.

    Where the SQLSTATE's message names the relation it is complaining about, that
    name has to be ours as well, and it has to be ours EXACTLY. See
    `_message_names_our_table`.
    """
    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate not in TABLE_UNAVAILABLE_SQLSTATES:
        return False
    if sqlstate in SQLSTATES_NAMING_THE_RELATION:
        return _message_names_our_table(sqlstate, error, cause)
    return True


def _message_names_our_table(sqlstate: str, error: Exception, cause) -> bool:
    """Whether the server said the missing or forbidden relation is OURS.

    **Do not simplify this to `SECONDARY_CATEGORIES_TABLE in str(error)`.** Real
    Postgres errors evade containment two ways, both demonstrated live: superstring
    relation names (`..._v2`, `_old`, `_backup`, `wsm_...`, which are exactly what
    PartsLogic's rename-and-swap rebuild produces), and the `LINE n:` echo of the
    failing statement, which by construction names our table even when the error is
    about a different relation. So: `diag.message_primary` (the server's first
    line, which carries neither), an anchored pattern, and EQUALITY. An optional
    schema qualification is allowed.

    ponytail: two regexes and an equality, not a SQL error parser. `diag.table_name`
    is the obvious structured reach and is **empty** for both states on Postgres 16
    with psycopg 3.2.9 (measured), so there is nothing to read. Ceiling: the wire
    text of one server family. A non-Postgres backend fails SAFE, by not
    recognising a verdict and letting the error surface. See FORK-NOTES.md.
    """
    diagnostics = getattr(cause, "diag", None)
    primary = getattr(diagnostics, "message_primary", None)
    if primary:
        candidates = [primary]
    else:
        # No structured diagnostics (a hand-built error, or a driver that does not
        # supply them). Django copies the driver's text onto its own exception, so
        # look at both, and only ever at the FIRST line, which is where the server
        # puts its verdict and where the LINE echo is not.
        candidates = [str(error).split("\n", 1)[0]]
        if cause is not None:
            candidates.append(str(cause).split("\n", 1)[0])
    for text in candidates:
        match = RELATION_IN_MESSAGE[sqlstate].search(text)
        if match is None:
            continue
        name = match.group("name").strip().rstrip(",.;").strip('"')
        # `schema.table` and `"schema"."table"` both reduce to the table identifier
        if name.split(".")[-1].strip('"') == SECONDARY_CATEGORIES_TABLE:
            return True
    return False


class ProductSecondaryCategory(models.Model):
    """Extra category assignments for a product, beyond its primary category.

    Created and populated by PartsLogic and the WSM 5.0 migration ETL, which write
    directly to this database; Saleor only reads it. Unmanaged, so the migration is
    state-only and emits no DDL, and absent entirely on tenants that never ran
    PartsLogic: call `secondary_categories_available()` before naming it in a query.

    **Do not declare these as Django `ForeignKey`s.** Plain integer columns add no
    reverse accessors to `Product` or `Category`, so nothing enters Django's delete
    collector and no dataloader or serializer changes shape. The database enforces
    referential integrity with its own FKs and `ON DELETE CASCADE`.
    """

    # The real column is `bigserial`. Declared explicitly because
    # settings.DEFAULT_AUTO_FIELD is AutoField, which would silently truncate on
    # a tenant whose psc sequence has passed 2^31. Nothing here selects `id`
    # today, so this is about the next person who does.
    id = models.BigAutoField(primary_key=True)
    product_id = models.BigIntegerField()
    category_id = models.BigIntegerField()

    class Meta:
        managed = False
        db_table = "product_secondary_categories"


def secondary_categories_available(database_connection_name: str) -> bool:
    """Whether `product_secondary_categories` exists, is readable and holds rows.

    Cached per connection alias for `PROBE_TTL_SECONDS`. The alias is a safe cache
    key only because WSM runs one Saleor process per tenant; it would be the wrong
    key under a shared-process router or schema-per-tenant.

    **Do not let the read side trust this answer blindly:** it can be stale for up
    to one interval, which is why `MultiLegQuerySet._degrading` exists. See
    FORK-NOTES.md.
    """
    if not SECONDARY_CATEGORIES_ENABLED:
        return False
    cached = _probe_cache.get(database_connection_name)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    available = _probe_secondary_categories(database_connection_name)
    _probe_cache[database_connection_name] = (
        available,
        time.monotonic() + PROBE_TTL_SECONDS,
    )
    return available


def disable_secondary_categories(database_connection_name: str) -> None:
    """Report unavailable for one TTL, without re-probing first.

    A statement that named the table has already failed, which is more
    authoritative than any probe. Do not merely invalidate the cache here: caching
    the negative answer is what keeps the failure to a single request rather than
    one per request until the TTL rolls over.
    """
    _probe_cache[database_connection_name] = (
        False,
        time.monotonic() + PROBE_TTL_SECONDS,
    )


def clear_secondary_categories_cache() -> None:
    """Drop every cached probe result. For tests."""
    _probe_cache.clear()


def _probe_secondary_categories(database_connection_name: str) -> bool:
    """Existence, then readability, then population. Three statements, in order.

    Existence because the table is absent on every tenant that never ran
    PartsLogic; the `SELECT` grant because PartsLogic's own Go migrations create
    the table, so nothing guarantees Saleor's role can read it (and asking
    `has_table_privilege` rather than catching a permission error keeps the failed
    case from aborting the surrounding transaction); population because several of
    the largest tenants hold the table with zero rows.

    Do not add an index check here. Whether a leading-`category_id` index exists is
    a performance property, not a correctness one, and turning the feature off for
    it would trade a slower page for a wrong one. See FORK-NOTES.md.
    """
    try:
        with (
            recoverable_failure(database_connection_name),
            connections[database_connection_name].cursor() as cursor,
        ):
            cursor.execute("SELECT to_regclass('product_secondary_categories')::oid")
            table_oid = cursor.fetchone()[0]
            if table_oid is None:
                return False
            cursor.execute("SELECT has_table_privilege(%s::oid, 'SELECT')", [table_oid])
            if not cursor.fetchone()[0]:
                return False
            cursor.execute("SELECT EXISTS (SELECT 1 FROM product_secondary_categories)")
            return cursor.fetchone()[0]
    except DatabaseError:
        # Belt and braces for anything the checks above cannot anticipate, the
        # real one being a drop between the privilege check and the population
        # check. The feature is an addition to a listing; nothing about it
        # justifies failing the request. Wrapped in `recoverable_failure` above,
        # because swallowing a failure inside a transaction block without one
        # would leave the connection aborted and turn this into the 500 the
        # whole guard exists to avoid.
        logger.warning(
            "%s probe failed; treating the feature as unavailable",
            SECONDARY_CATEGORIES_TABLE,
            exc_info=True,
        )
        return False
