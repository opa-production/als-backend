import uuid
from datetime import datetime, time
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _clip(limit: int):
    """
    A title too long trims instead of failing the whole push.

    Sync is all-or-nothing: one bad row 422s the entire batch, so a single
    material whose title came from a long filename stopped a device syncing
    anything at all, forever. It retried every few seconds and failed the
    same way each time.

    Titles are display strings, not data anyone computes on, so clipping one
    is a smaller harm than blocking every note, event and chat behind it.
    This is deliberately not applied to ids, timestamps or foreign keys --
    those must still be rejected loudly, because guessing at them corrupts
    the very thing sync exists to protect.
    """

    def clip(value: object) -> object:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit]
        return value

    return BeforeValidator(clip)


#: Titles arrive from filenames and user typing, and both overrun.
Title300 = Annotated[str, _clip(300), Field(max_length=300)]


class SyncRow(BaseModel):
    """
    What every synced row carries.

    ``id`` comes from the device and ``updated_at`` is the whole conflict
    resolution story — see ``app/services/sync.py``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    updated_at: datetime
    deleted_at: datetime | None = None


class UnitIn(SyncRow):
    code: str = Field(max_length=16)
    title: str = Field(max_length=200)
    lecturer: str = Field(default="", max_length=160)


class ClassSessionIn(SyncRow):
    unit_id: uuid.UUID
    #: 0 = Sunday, matching JavaScript's getDay().
    weekday: int = Field(ge=0, le=6)
    starts_at: time
    ends_at: time
    room: str = Field(default="", max_length=80)


class MaterialIn(SyncRow):
    unit_id: uuid.UUID
    kind: str = Field(default="note", max_length=16)
    title: Title300
    body: str = ""
    archived: bool = False


class EventIn(SyncRow):
    unit_id: uuid.UUID | None = None
    title: Title300
    kind: str = Field(default="assignment", max_length=16)
    label: str = Field(default="", max_length=80)
    due_at: datetime | None = None
    done: bool = False


class MessageIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str = Field(max_length=16)
    content: str
    sources: list | None = None
    created_at: datetime


class ChatIn(SyncRow):
    unit_id: uuid.UUID | None = None
    title: str = Field(default="New chat", max_length=120)
    messages: list[MessageIn] = Field(default_factory=list)


class SyncPush(BaseModel):
    """
    Everything a device wants to write, in one request.

    One request rather than one per table: a phone coming back from a day
    offline has changes in several tables that belong to the same moment, and
    six requests over a bad connection is six chances to half-succeed.
    """

    units: list[UnitIn] = Field(default_factory=list)
    class_sessions: list[ClassSessionIn] = Field(default_factory=list)
    materials: list[MaterialIn] = Field(default_factory=list)
    events: list[EventIn] = Field(default_factory=list)
    chats: list[ChatIn] = Field(default_factory=list)


class TableResult(BaseModel):
    applied: int = 0
    #: Rows the server already had a newer version of. Not an error — the
    #: device simply had stale data, and knowing the count makes a sync that
    #: quietly does nothing debuggable.
    skipped: int = 0
    rejected: list[str] = Field(default_factory=list)


class SyncPushResult(BaseModel):
    units: TableResult = Field(default_factory=TableResult)
    class_sessions: TableResult = Field(default_factory=TableResult)
    materials: TableResult = Field(default_factory=TableResult)
    events: TableResult = Field(default_factory=TableResult)
    chats: TableResult = Field(default_factory=TableResult)

    #: Pass back as ``since`` on the next pull.
    cursor: datetime


class UnitOut(UnitIn):
    pass


class ClassSessionOut(ClassSessionIn):
    pass


class MaterialOut(MaterialIn):
    storage_bucket: str | None = None
    storage_path: str | None = None
    page_count: int | None = None
    extraction_status: str = "pending"
    #: Why it failed, in words meant for the student.
    #:
    #: Server-authored and read-only — it is absent from `MaterialIn`, so a
    #: device cannot write one, and sync never copies it back.
    #:
    #: Without this the app received `failed` and nothing else, and a card that
    #: renders anything other than `done` as "still reading" showed a permanent
    #: spinner over a document that had already been rejected with a perfectly
    #: good explanation — "it looks like a scan", "that PDF is password
    #: protected" — sitting unread in a column.
    extraction_error: str | None = None


class EventOut(EventIn):
    pass


class ChatOut(ChatIn):
    pass


class SyncPull(BaseModel):
    """
    Everything that changed after the cursor, tombstones included.

    Deletions travel as rows with ``deleted_at`` set. A row that simply
    vanished would be invisible to a device that has been offline, which would
    then push it straight back.
    """

    units: list[UnitOut] = Field(default_factory=list)
    class_sessions: list[ClassSessionOut] = Field(default_factory=list)
    materials: list[MaterialOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    chats: list[ChatOut] = Field(default_factory=list)

    cursor: datetime
    #: True when the page hit the row limit and another pull is needed.
    has_more: bool = False
