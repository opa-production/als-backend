import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, MetaData, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: JSONB on Postgres, plain JSON on SQLite.
#:
#: The variant exists so the test suite can run against in-memory SQLite with
#: no container. Postgres still gets JSONB and everything that comes with it —
#: nothing is given up in the environment that matters.
JsonB = JSONB().with_variant(JSON(), "sqlite")

#: SQLAlchemy's own UUID type rather than the Postgres-only one: it renders a
#: native `uuid` column on Postgres and CHAR(32) elsewhere, for the same reason.
UuidPK = Uuid(as_uuid=True)

#: Explicit constraint names.
#:
#: Without this, Postgres invents names and Alembic autogenerate cannot then
#: drop or alter a constraint it did not create — you end up hand-writing
#: migrations to rename things. Set once, before the first migration, because
#: changing it later renames every constraint in the database.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    """
    A primary key the *client* chooses.

    This is the single most important decision in the schema. The mobile app
    mints ids with ``crypto.randomUUID`` before a row has ever reached the
    server, which is what makes sync retries safe: pushing the same note twice
    is one upsert on one id, not two rows. A server-side default would turn
    every retry on a flaky connection into a duplicate.

    The default here is only for rows the server originates.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UuidPK,
        primary_key=True,
        default=uuid.uuid4,
    )


class Timestamps:
    """
    Server-side timestamps, in UTC.

    ``server_default=func.now()`` rather than a Python default: the database
    clock is the only one every writer agrees on, and it stays correct for rows
    inserted by a migration or by hand.

    ``updated_at`` is what sync pages on — ``GET /sync?since=`` walks it — so
    it is indexed per table where that matters.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDelete:
    """
    Deletion as a tombstone.

    A row hard-deleted on the server is invisible to a device that has been
    offline — it simply never hears, and pushes the row back on next sync. A
    ``deleted_at`` that syncs like any other change is what lets a delete
    actually propagate.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
