"""
Fails if the models and the migrations have drifted apart.

The failure this exists to catch: someone adds a column to a model, the tests
pass against a schema built from ``Base.metadata``, and nobody generates a
migration. Deploy runs ``alembic upgrade head``, which succeeds without doing
anything, and production is left one column behind its own code.

Runs without a database. ``alembic upgrade head --sql`` compiles every
migration to Postgres DDL as text, so the tables and columns the migrations
actually create can be read straight out of it and compared to the models.

    python scripts/check_migrations.py
"""

from __future__ import annotations

import re
import subprocess
import sys

from app.models import Base

# CREATE TABLE <name> (\n ... \n);  -- non-greedy, anchored on the closing line
_CREATE_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.S)
_ADD_COLUMN = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+)")
_DROP_COLUMN = re.compile(r"ALTER TABLE (\w+) DROP COLUMN (\w+)")

#: Lines inside a CREATE TABLE body that declare a constraint rather than a
#: column, and so must not be mistaken for one.
_CONSTRAINT_KEYWORDS = {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}


def _compile_migrations() -> str:
    """Renders every migration to DDL text. Needs no database connection."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("alembic could not compile the migrations:\n", result.stderr)
        raise SystemExit(1)
    return result.stdout


def _schema_from_ddl(sql: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}

    for match in _CREATE_TABLE.finditer(sql):
        name, body = match.group(1), match.group(2)
        columns = set()
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line:
                continue
            first = line.split()[0]
            if first.upper() in _CONSTRAINT_KEYWORDS:
                continue
            columns.add(first)
        tables[name] = columns

    # Columns added or removed by later migrations, not present in any
    # CREATE TABLE.
    for name, column in _ADD_COLUMN.findall(sql):
        tables.setdefault(name, set()).add(column)
    for name, column in _DROP_COLUMN.findall(sql):
        tables.get(name, set()).discard(column)

    # Alembic's own bookkeeping table is not part of the model set.
    tables.pop("alembic_version", None)
    return tables


def main() -> int:
    migrated = _schema_from_ddl(_compile_migrations())
    modelled = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }

    problems: list[str] = []

    for table, columns in modelled.items():
        if table not in migrated:
            problems.append(f"table {table!r} exists in the models but no migration creates it")
            continue
        missing = columns - migrated[table]
        if missing:
            problems.append(
                f"table {table!r} is missing {sorted(missing)} in the migrations"
            )

    for table in migrated.keys() - modelled.keys():
        problems.append(f"table {table!r} is created by a migration but has no model")

    for table in modelled.keys() & migrated.keys():
        extra = migrated[table] - modelled[table]
        if extra:
            problems.append(
                f"table {table!r} has {sorted(extra)} in the migrations but not in the models"
            )

    print(f"models: {len(modelled)} tables | migrations: {len(migrated)} tables")

    if problems:
        print("\nSCHEMA DRIFT")
        for problem in problems:
            print(f"  - {problem}")
        print("\nGenerate the missing migration with:")
        print("    alembic revision --autogenerate -m '<what changed>'")
        return 1

    print("in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
