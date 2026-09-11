# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The house rule as a test: the fork ADDS tables and never modifies Saleor's.

Three halves. Outside `saleor/wsm/`, only settings.py and urls.py may differ from
the upstream release this fork sits on, so no core model or core migration can
have moved. Inside it, every operation that reaches the DATABASE must name a
`wsm_` table, and no migration may run arbitrary Python without saying in the
file why that is safe. State-only operations name nothing, which is the point of
them, and an unmanaged model is state-only: it declares a table another service
owns. Third, the set of monkey patches actually installed must be the one
CORE-TOUCHES.md claims, so no patch lands against a doc that never mentions it.

A guard is only worth its runtime if it cannot be walked past. Two ways it could
be, both closed here: `RunPython` reaches the database through an ORM the
operation never names, and `RunSQL` accepts a LIST of statements (or of
`(sql, params)` pairs), which the old flat `str(operation.sql)` tokenised into
nothing.
"""

import importlib
import subprocess
from pathlib import Path

from django.db import migrations
from django.db.migrations.loader import MigrationLoader

from .. import patches

UPSTREAM = "a1ab3a2"
# The two registration points the fork's own apps need, plus every core file
# Bill's six pre-existing WSM patches edit. Listed one by one, on purpose: this
# set is the fork's deviation budget, so growing it is a decision someone has to
# make in a diff, never something a patch can do by arriving.
ALLOWED_CORE_FILES = {
    # Fork app registration (U1-U4) and the ROOT_URLCONF seam that mounts the
    # composed GraphQL schema. `saleor/urls.py` used to be here; the fork's
    # urlconf includes it now instead of the other way round, so it is back to
    # upstream byte-for-byte and this budget is one file shorter.
    "saleor/settings.py",
    # WSM6-1978, legacy WSM5 password hashers, upgraded on first sign-in.
    "saleor/core/hashers.py",
    "saleor/core/tests/test_hashers.py",
    # WSM6-1178, NULL-price channel listings must not crash variant pricing.
    "saleor/graphql/product/types/products.py",
    # Braintree gateway crash on order details with missing credentials.
    "saleor/payment/gateways/braintree/plugin.py",
    "saleor/payment/gateways/braintree/tests/test_braintree.py",
    # An undeliverable destination is not the same error as missing stock.
    "saleor/checkout/error_codes.py",
    "saleor/core/exceptions.py",
    "saleor/graphql/checkout/mutations/checkout_create_from_order.py",
    "saleor/graphql/checkout/mutations/utils.py",
    "saleor/graphql/checkout/tests/mutations/test_checkout_create.py",
    "saleor/graphql/checkout/tests/mutations/test_checkout_lines_add.py",
    "saleor/graphql/checkout/tests/mutations/test_checkout_lines_update.py",
    "saleor/graphql/checkout/tests/mutations/test_checkout_shipping_address_update.py",
    "saleor/graphql/schema.graphql",
    "saleor/warehouse/availability.py",
    "saleor/warehouse/tests/test_stock_availability.py",
    # Validate a checkout is payment-ready before charging it.
    "saleor/checkout/checkout_cleaner.py",
    "saleor/checkout/complete_checkout.py",
    "saleor/checkout/tests/test_checkout_cleaner.py",
    "saleor/graphql/checkout/tests/mutations/test_checkout_complete_with_transactions.py",
    "saleor/graphql/payment/mutations/transaction/transaction_initialize.py",
    "saleor/graphql/payment/mutations/transaction/transaction_process.py",
}
REPO = Path(__file__).resolve().parents[3]
# The word before a table name in the SQL a migration is allowed to run.
SQL_TABLE_WORDS = ("table", "into", "update", "from", "join")
# A `RunPython` is allowed only where the migration file says why, on the line
# above the operation, in these exact words.
CORE_SAFE_MARKER = "# WSM-CORE-SAFE:"


def test_only_settings_and_urls_differ_from_upstream():
    changed = subprocess.run(
        ["git", "diff", "--name-only", UPSTREAM, "--", "saleor/", ":!saleor/wsm"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert set(changed) <= ALLOWED_CORE_FILES, changed


def _statements(sql):
    """`RunSQL.sql` flattened to strings. It may be one, a list, or (sql, params)."""
    if isinstance(sql, str):
        return [sql]
    if isinstance(sql, list | tuple):
        return [
            statement
            for item in sql
            for statement in _statements(
                item[0] if isinstance(item, list | tuple) and item else item
            )
        ]
    return [str(sql)]


def _tables(operation, app_label):
    """Every table this operation writes to. State-only operations write to none."""
    if isinstance(operation, migrations.SeparateDatabaseAndState):
        return [t for o in operation.database_operations for t in _tables(o, app_label)]
    if isinstance(operation, migrations.CreateModel):
        # `managed = False` means Django emits no DDL for this model, so the
        # operation is state-only in exactly the sense the docstring means: it
        # declares a table someone else owns (PartsLogic writes
        # `product_secondary_categories`) rather than creating one. Reading the
        # option rather than the table name is what keeps this half strict:
        # a managed model still has to be `wsm_`-prefixed.
        if operation.options.get("managed") is False:
            return []
        return [
            operation.options.get("db_table") or f"{app_label}_{operation.name.lower()}"
        ]
    if isinstance(operation, migrations.AlterModelTable):
        return [operation.table or f"{app_label}_{operation.name.lower()}"]
    if isinstance(operation, migrations.RunSQL):
        found = []
        for statement in _statements(operation.sql) + _statements(
            operation.reverse_sql
        ):
            words = statement.replace(";", " ").replace("(", " ").split()
            found += [
                w.strip('"')
                for i, w in enumerate(words[1:])
                if words[i].lower() in SQL_TABLE_WORDS
            ]
        return found
    model_name = getattr(operation, "model_name", None)
    return [f"{app_label}_{model_name.lower()}"] if model_name else []


def _flatten(operations):
    for operation in operations:
        yield operation
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _flatten(operation.database_operations)


def _fork_migrations():
    for (app_label, name), migration in MigrationLoader(None).disk_migrations.items():
        module = importlib.import_module(migration.__class__.__module__)
        path = Path(module.__file__)
        if "/saleor/wsm/" not in path.as_posix():
            continue
        yield app_label, name, path, migration


def test_fork_migrations_only_create_wsm_tables():
    seen = []
    for app_label, name, _path, migration in _fork_migrations():
        for operation in migration.operations:
            for table in _tables(operation, app_label):
                assert table.startswith("wsm_"), f"{app_label}.{name} writes to {table}"
                seen.append(table)
    assert seen, "no fork migrations were scanned, so this test proved nothing"


def test_fork_migrations_do_not_run_undeclared_python():
    """`RunPython` names no table, so the check above cannot see what it touches.

    The migration may still have one, but it has to say so IN THE FILE, one line
    above the operation, where the person writing the rebase will read it.
    """
    scanned = 0
    for app_label, name, path, migration in _fork_migrations():
        scanned += 1
        if not any(
            isinstance(op, migrations.RunPython)
            for op in _flatten(migration.operations)
        ):
            continue
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if "RunPython" not in line:
                continue
            above = lines[index - 1] if index else ""
            assert CORE_SAFE_MARKER in above, (
                f"{app_label}.{name} line {index + 1} runs Python with no "
                f"'{CORE_SAFE_MARKER} <reason>' line above it"
            )
    assert scanned, "no fork migrations were scanned, so this test proved nothing"


def test_the_installed_monkey_patches_are_the_documented_ones():
    """Nothing wraps a core function without a line in `saleor/wsm/patches.py`.

    Subset, not equality: PINNED names the patches of every branch in flight, so
    a branch carrying only some of them still passes here, while a patch nobody
    wrote down fails, in the same run as the doc it should have been added to.
    """
    installed = patches.installed()
    pinned = frozenset(patches.PINNED)

    assert installed, "no monkey patch was discovered, so this test proved nothing"
    assert installed <= pinned, (
        f"undocumented monkey patch: {sorted(installed - pinned)}. Add it to "
        "saleor/wsm/patches.py PINNED and to docs/wsm/CORE-TOUCHES.md."
    )


def test_the_guard_names_a_planted_patch_even_beside_a_test_double():
    """`installed()` has to survive the namespace the full suite leaves behind.

    Two failures in one test, because the fix for the second must not buy its
    green by blinding the first. A wrapper planted in a core module namespace
    has to be NAMED, or the pin is decoration. And a test double left in that
    same namespace answers EVERY attribute, `__code__` included, so a sweep
    that trusts the answer dies on `co_filename`: under the full suite the
    guard then errors instead of reporting, which is the loudest possible way
    to stop guarding. The double is planted first and the assertion is made
    with it still in place.
    """
    import functools
    from unittest import mock

    from saleor.checkout import calculations

    @functools.wraps(calculations.fetch_checkout_data)
    def undocumented(*args, **kwargs):  # pragma: no cover - never called
        return calculations.fetch_checkout_data(*args, **kwargs)

    planted = "saleor.checkout.calculations.fetch_checkout_data"
    assert planted not in frozenset(patches.PINNED), (
        "this test needs a core function the fork does NOT patch"
    )

    class AnswersAnything:
        """A test double of the shape the full suite leaves lying around.

        It answers `__wrapped__` and `__code__` like a wrapper does, with a
        sentinel, so a sweep that trusts the answer reaches `co_filename` on an
        object that has none.
        """

        def __getattr__(self, name):
            return mock.sentinel.whatever_was_asked_for

    with mock.patch.object(
        calculations, "wsm_guard_probe_double", AnswersAnything(), create=True
    ):
        assert planted not in patches.installed()

        with mock.patch.object(
            calculations, "wsm_guard_probe_patch", undocumented, create=True
        ):
            assert planted in patches.installed()


def test_the_installed_core_class_extensions_are_the_documented_ones():
    """MP4 is not a wrapped function, so the patch sweep above cannot see it.

    `saleor/wsm/graphql/compose/product_extension.py` appends fields to stock's
    `Product` graphene type at import: additive, idempotent and ordered, but a
    mutation of a core CLASS all the same, and the ledger said "three patches,
    nothing here adds one". It is the fourth entry now, with the same shape of
    tripwire the other three have: the real set is DISCOVERED off the core
    types and compared against the pin, so an extension that lands without a
    line here reddens this test in the run that adds it.
    """
    discovered = patches.extensions_installed()

    assert discovered, "no core class extension was discovered, so this proved nothing"
    assert discovered == patches.EXTENDED, (
        f"core class extensions {sorted(discovered.items())} do not match the "
        f"pin {sorted(patches.EXTENDED.items())}. Update saleor/wsm/patches.py "
        "EXTENDED and docs/wsm/CORE-TOUCHES.md."
    )


def test_the_core_attributes_this_fork_rebinds_are_the_documented_ones():
    """MP5: one module attribute repointed, discovered rather than declared.

    `saleor/graphql/views.py` parses a document with the VIEW's schema and costs
    it against the module global it imported from `saleor.graphql.api`. On stock
    Saleor those are one object; on this fork the view serves the composed
    schema, so the complexity guard was weighing a schema with no `Wsm*` field
    in it. `saleor/wsm/graphql/cost.py` repoints that one name. The pin below is
    compared against what is actually bound, so a second rebind cannot arrive
    quietly.
    """
    # The composed schema is built, and the rebind installed, on import.
    from saleor.wsm.graphql import schema as wsm_schema

    assert wsm_schema.schema is not None

    discovered = patches.rebindings_installed()

    assert discovered, "no rebinding was discovered, so this test proved nothing"
    assert discovered == patches.REBOUND, (
        f"core attributes {sorted(discovered)} do not match the pin "
        f"{sorted(patches.REBOUND)}. Update saleor/wsm/patches.py REBOUND and "
        "docs/wsm/CORE-TOUCHES.md."
    )
