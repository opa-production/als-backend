"""
Delivering a notification to a handset.

Expo rather than APNs and FCM directly, because the app is an Expo client and
the tokens already stored on ``devices`` are Expo tokens. That trade is worth
naming: one HTTP endpoint and no certificates to rotate, at the cost of a hop
this service does not run. If that hop ever becomes the problem, only this
module changes — nothing above it knows what a push token looks like.

Same shape as ``app/services/sms.py``: a real provider when credentials exist,
one that logs otherwise, so the entire reminder path is exercisable locally
with no Expo project and no phone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()

#: Expo's documented ceiling for one request. Sending more in a single POST is
#: rejected outright, so the chunking is not an optimisation.
CHUNK = 100

#: What Expo says when a token belongs to an app that has been uninstalled or
#: whose token has been reissued. It is the only error worth acting on: the
#: token is dead and will never work again, so it is cleared rather than
#: retried forever on every sweep.
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


@dataclass(slots=True)
class PushMessage:
    """One notification, addressed to one device."""

    token: str
    title: str
    body: str
    #: Delivered to the app as ``notification.request.content.data`` — this is
    #: what lets a tap open the right screen rather than just the app.
    data: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PushResult:
    """
    What happened, per message, in the order they were given.

    ``dead_tokens`` is separate because it is the only outcome the caller must
    act on: those rows need their ``push_token`` cleared.
    """

    ok: list[bool]
    errors: list[str]
    dead_tokens: set[str] = field(default_factory=set)

    @property
    def sent(self) -> int:
        return sum(self.ok)


def looks_like_expo_token(token: str | None) -> bool:
    """
    Cheap sanity check before spending a round trip.

    A device that has not been granted notification permission sometimes
    registers with an empty string or a raw FCM token; Expo rejects both, and
    catching it here keeps that failure out of the log every single minute.
    """
    if not token:
        return False
    return token.startswith(("ExponentPushToken[", "ExpoPushToken[")) and token.endswith("]")


class PushProvider:
    async def send(self, messages: list[PushMessage]) -> PushResult:  # pragma: no cover
        raise NotImplementedError


class ConsolePushProvider(PushProvider):
    """
    Logs instead of sending.

    Used whenever Expo is unconfigured, which is every development machine. The
    reminder logic above it is fully exercised — what would have been sent
    appears in the journal — so the only untested part is the HTTP call itself.
    """

    async def send(self, messages: list[PushMessage]) -> PushResult:
        for message in messages:
            log.info(
                "push_not_sent_no_provider",
                title=message.title,
                body=message.body,
                data=message.data,
            )
        return PushResult(ok=[True] * len(messages), errors=[""] * len(messages))


class ExpoPushProvider(PushProvider):
    """
    Expo's push service.

    Two things about their API drive this code. It answers 200 with per-message
    errors in the body, so the status line alone never tells you whether a
    notification left the building — the same trap as the SMS gateway. And it
    caps a request at 100 messages, so anything larger has to be chunked.

    Receipts (the second, asynchronous half of Expo's protocol) are deliberately
    not polled. They arrive minutes later and would need their own table and
    sweep; the error that matters for a dead token already comes back in the
    immediate response, which is the one this service can act on.
    """

    ENDPOINT = "https://exp.host/--/api/v2/push/send"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, messages: list[PushMessage]) -> PushResult:
        result = PushResult(ok=[], errors=[])

        for start in range(0, len(messages), CHUNK):
            await self._send_chunk(messages[start : start + CHUNK], result)

        return result

    async def _send_chunk(self, chunk: list[PushMessage], result: PushResult) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.expo_access_token:
            # Only required when the Expo project has enhanced security turned
            # on, but harmless otherwise, so it is sent whenever configured.
            headers["Authorization"] = f"Bearer {settings.expo_access_token}"

        payload = [
            {
                "to": message.token,
                "title": message.title,
                "body": message.body,
                "data": message.data,
                "sound": "default",
                # Reminders are time-critical and worthless late: a nudge for a
                # 4pm class delivered at 6 is worse than none at all.
                "priority": "high",
                "ttl": settings.push_ttl_seconds,
            }
            for message in chunk
        ]

        try:
            response = await self._client.post(
                self.ENDPOINT, json=payload, headers=headers
            )
        except httpx.HTTPError as error:
            # Unreachable is not the same as rejected: the tokens are fine, so
            # nothing is cleared and the next sweep tries again.
            log.error("push_send_unreachable", count=len(chunk), error=str(error))
            result.ok.extend([False] * len(chunk))
            result.errors.extend(["unreachable"] * len(chunk))
            return

        if response.status_code >= 400:
            log.error(
                "push_send_rejected",
                status=response.status_code,
                detail=response.text[:300],
            )
            result.ok.extend([False] * len(chunk))
            result.errors.extend([f"http {response.status_code}"] * len(chunk))
            return

        self._read_tickets(response, chunk, result)

    def _read_tickets(
        self, response: httpx.Response, chunk: list[PushMessage], result: PushResult
    ) -> None:
        """
        Reads Expo's answer without trusting its shape.

        A 200 whose body is not the documented list fails the whole chunk rather
        than raising, because this runs inside a sweep that has to survive one
        bad response.
        """
        try:
            payload = response.json()
        except ValueError:
            payload = None

        tickets = payload.get("data") if isinstance(payload, dict) else None

        if not isinstance(tickets, list) or len(tickets) != len(chunk):
            log.error("push_send_unreadable", detail=str(payload)[:300])
            result.ok.extend([False] * len(chunk))
            result.errors.extend(["unreadable response"] * len(chunk))
            return

        for message, ticket in zip(chunk, tickets, strict=True):
            if isinstance(ticket, dict) and ticket.get("status") == "ok":
                result.ok.append(True)
                result.errors.append("")
                continue

            detail = ""
            code = ""
            if isinstance(ticket, dict):
                detail = str(ticket.get("message", ""))[:300]
                details = ticket.get("details")
                code = str(details.get("error", "")) if isinstance(details, dict) else ""

            if code == DEVICE_NOT_REGISTERED:
                result.dead_tokens.add(message.token)

            log.warning("push_ticket_failed", detail=detail, code=code)
            result.ok.append(False)
            result.errors.append(detail or code or "rejected")

        log.info("push_sent", count=result.sent, of=len(chunk))


def get_push_provider(client: httpx.AsyncClient) -> PushProvider:
    if settings.push_configured:
        return ExpoPushProvider(client)
    return ConsolePushProvider()


def notification_data(kind: str, subject_id: uuid.UUID | None) -> dict[str, str]:
    """
    The payload a tap carries back into the app.

    Kept to strings: Expo serialises this to JSON and the client reads it out of
    a native payload, where a UUID object has no meaning.
    """
    data = {"kind": kind}
    if subject_id is not None:
        data["id"] = str(subject_id)
    return data
