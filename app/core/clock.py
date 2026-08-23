from datetime import UTC, datetime


def now() -> datetime:
    """The current instant, always timezone-aware and always UTC."""
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """
    Guarantees a datetime read back from the database is comparable.

    Postgres ``TIMESTAMPTZ`` returns an aware datetime, but not every driver
    or backend does — SQLite has no timezone type at all, so a value written
    as aware comes back naive. Comparing the two raises ``TypeError``, and it
    raises it deep inside a request rather than anywhere near the cause.

    A naive value is assumed to be UTC, which is true because every write in
    this service is UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
