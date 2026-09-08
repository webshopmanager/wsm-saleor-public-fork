# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The house rule as a test: the fork ADDS tables and never modifies Saleor's.

Three halves. Outside `saleor/wsm/`, only settings.py and urls.py may differ from
the upstream release this fork sits on, so no core model or core migration can
have moved. Inside it, every operation that reaches the DATABASE must name a
`wsm_` table, and no migration may run arbitrary Python without saying in the
file why that is safe. Third, the set of monkey patches actually installed must
be the one CORE-TOUCHES.md claims, so no patch lands against a doc that never mentions it.

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
ALLOWED_CORE_FILES = {"saleor/settings.py", "saleor/urls.py"}
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
            for statement in _statements(item[0] if isinstance(item, list | tuple) and item else item)
        ]
    return [str(sql)]


def _tables(operation, app_label):
    """Every table this operation writes to. State-only operations write to none."""
    if isinstance(operation, migrations.SeparateDatabaseAndState):
        return [t for o in operation.database_operations for t in _tables(o, app_label)]
    if isinstance(operation, migrations.CreateModel):
        return [operation.options.get("db_table") or f"{app_label}_{operation.name.lower()}"]
    if isinstance(operation, migrations.AlterModelTable):
        return [operation.table or f"{app_label}_{operation.name.lower()}"]
    if isinstance(operation, migrations.RunSQL):
        found = []
        for statement in _statements(operation.sql) + _statements(operation.reverse_sql):
            words = statement.replace(";", " ").replace("(", " ").split()
            found += [w.strip('"') for i, w in enumerate(words[1:]) if words[i].lower() in SQL_TABLE_WORDS]
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
            isinstance(op, migrations.RunPython) for op in _flatten(migration.operations)
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
