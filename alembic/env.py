import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings

# Importing the package registers every table on Base.metadata. Without it,
# autogenerate sees an empty model set and writes a migration that drops the
# entire schema.
from app.models import Base  # noqa: F401

config = context.config

# The URL comes from the environment, never from alembic.ini — a committed
# connection string is a committed credential.
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    """
    Keeps extensions and anything Supabase manages out of our migrations.

    A Supabase database has schemas this service does not own (``auth``,
    ``storage``, ``realtime``). Without this filter, autogenerate proposes
    dropping all of them.
    """
    if type_ == "table" and getattr(object_, "schema", None) not in (None, "public"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emits SQL to stdout for review, without touching a database."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        # Catches a column whose type or default drifted from the model, which
        # is otherwise invisible until something writes the wrong value.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Runs migrations over the same asyncpg driver the app uses.

    NullPool because this process runs one DDL transaction and exits — a pool
    would just leave connections open against a database the release step is
    about to hand over to the app.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
