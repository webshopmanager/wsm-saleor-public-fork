# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The house rule as a test: the fork ADDS tables and never modifies Saleor's.

Two halves. Outside `saleor/wsm/`, only settings.py and urls.py may differ from
the upstream release this fork sits on, so no core model or core migration can
have moved. Inside it, every operation that reaches the DATABASE must name a
`wsm_` table. State-only operations name nothing, which is the point of them.
"""

import importlib
import subprocess
from pathlib import Path

from django.db import migrations
from django.db.migrations.loader import MigrationLoader

UPSTREAM = "a1ab3a2"
ALLOWED_CORE_FILES = {"saleor/settings.py", "saleor/urls.py"}
REPO = Path(__file__).resolve().parents[3]
# The word before a table name in the SQL a migration is allowed to run.
SQL_TABLE_WORDS = ("table", "into", "update", "from", "join")


def test_only_settings_and_urls_differ_from_upstream():
    changed = subprocess.run(
        ["git", "diff", "--name-only", UPSTREAM, "--", "saleor/", ":!saleor/wsm"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert set(changed) <= ALLOWED_CORE_FILES, changed


def _tables(operation, app_label):
    """Every table this operation writes to. State-only operations write to none."""
    if isinstance(operation, migrations.SeparateDatabaseAndState):
        return [t for o in operation.database_operations for t in _tables(o, app_label)]
    if isinstance(operation, migrations.CreateModel):
        return [operation.options.get("db_table") or f"{app_label}_{operation.name.lower()}"]
    if isinstance(operation, migrations.AlterModelTable):
        return [operation.table or f"{app_label}_{operation.name.lower()}"]
    if isinstance(operation, migrations.RunSQL):
        words = str(operation.sql).replace(";", " ").replace("(", " ").split()
        return [w.strip('"') for i, w in enumerate(words[1:]) if words[i].lower() in SQL_TABLE_WORDS]
    model_name = getattr(operation, "model_name", None)
    return [f"{app_label}_{model_name.lower()}"] if model_name else []


def test_fork_migrations_only_create_wsm_tables():
    seen = []
    for (app_label, name), migration in MigrationLoader(None).disk_migrations.items():
        module = importlib.import_module(migration.__class__.__module__)
        if "/saleor/wsm/" not in Path(module.__file__).as_posix():
            continue
        for operation in migration.operations:
            for table in _tables(operation, app_label):
                assert table.startswith("wsm_"), f"{app_label}.{name} writes to {table}"
                seen.append(table)
    assert seen, "no fork migrations were scanned, so this test proved nothing"
