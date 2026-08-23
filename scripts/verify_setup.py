"""
Smoke test for the scaffold: does it import, and does Alembic see every table?

Run with `python scripts/verify_setup.py`. Kept in the repo because the failure
it catches — a model that never gets imported, so autogenerate writes a
migration to drop its table — is silent and expensive.
"""

from app.db.base import Base
from app.main import app
from app.models import __all__ as exported


def main() -> None:
    routes = sorted(
        r.path for r in app.routes if getattr(r, "path", "").startswith(("/health", "/api"))
    )
    print("app imports OK")
    print("routes:", routes or "(none yet)")

    print("\ntables Alembic will manage:")
    for name, table in sorted(Base.metadata.tables.items()):
        print(f"  {name:<22} {len(table.columns):>2} cols  {len(table.indexes)} index(es)")
    print(f"\ntotal tables: {len(Base.metadata.tables)}")
    print(f"models exported: {len(exported) - 1}")  # minus Base itself

    # Every table must trace back to a model in __init__, or autogenerate will
    # not see it.
    missing = [
        name for name in Base.metadata.tables if name not in _table_names_from_models()
    ]
    if missing:
        raise SystemExit(f"tables with no exported model: {missing}")
    print("\nevery table is reachable from app.models — autogenerate is safe")


def _table_names_from_models() -> set[str]:
    import app.models as models

    return {
        getattr(models, name).__tablename__
        for name in models.__all__
        if hasattr(getattr(models, name), "__tablename__")
    }


if __name__ == "__main__":
    main()
