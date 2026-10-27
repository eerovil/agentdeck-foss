"""A column added to a shipped table must come with an ordered migration (#2601).

``CREATE TABLE IF NOT EXISTS`` never reaches a table that already exists, and
nothing infers missing columns any more, so a column declared in ``_SCHEMA``
without a numbered step in ``db_migrations.MIGRATIONS`` would exist only on fresh
databases while every deployed deck silently stored nothing in it (#579).

``fixtures/db_schema_baseline.json`` is every table's columns when the inferred
upgrader was removed. It never needs regenerating: a brand-new table is created
whole and needs no migration, and a new column on a baseline table is exactly
what the first test catches.
"""

import json
from pathlib import Path

from agentdeck.db import _declared_columns
from agentdeck.db_migrations import MIGRATIONS

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "db_schema_baseline.json").read_text())


def _migrated() -> dict[tuple[str, str], str]:
    return {
        (table, column): declaration
        for migration in MIGRATIONS
        for table, column, declaration in migration.statements
    }


def test_every_column_added_to_a_shipped_table_has_a_migration():
    migrated = _migrated()
    missing = [
        f"{table}.{name}"
        for table, columns in _declared_columns().items()
        if table in BASELINE
        for name, *_rest in columns
        if name not in BASELINE[table] and (table, name) not in migrated
    ]
    assert not missing, "add an ordered migration in db_migrations.py for: " + ", ".join(missing)


def test_each_migration_adds_the_column_as_the_schema_declares_it():
    declared = {
        (table, name): (decl_type, notnull, default)
        for table, columns in _declared_columns().items()
        for name, decl_type, notnull, default in columns
    }
    for (table, column), declaration in _migrated().items():
        if (table, column) not in declared:
            continue  # a retired table keeps its historical step
        decl_type, notnull, default = declared[(table, column)]
        expected = decl_type
        if notnull:
            expected += " NOT NULL"
        if default is not None:
            expected += f" DEFAULT {default}"
        # SQLite cannot add a NOT NULL column without a default, so a step may supply
        # one the table itself does not declare (migration 5 does).
        backfilled = notnull and default is None and declaration.startswith(expected + " DEFAULT ")
        assert declaration == expected or backfilled, f"{table}.{column}"
