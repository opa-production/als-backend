from __future__ import annotations

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()


class SmsProvider:
    """
    Sending a text.

    An interface with a real implementation and a development one, chosen by
    whether credentials exist. The alternative — writing the OTP flow directly
    against a provider — means the whole signup path is untestable until an
    account is set up and funded, which is the wrong order to build in.
    """

    async def send(self, phone: str, message: str) -> None:  # pragma: no cover
        raise NotImplementedError


class ConsoleSmsProvider(SmsProvider):
    """
    Logs the message instead of sending it.

    Used whenever no provider is configured. The code appears in the server log,
    which is what makes the entire auth flow testable through Swagger with no
    account, no credit and no phone.
    """

    async def send(self, phone: str, message: str) -> None:
        log.info("sms_not_sent_no_provider", phone=phone, message=message)


class CelcomSmsProvider(SmsProvider):
    """
    Celcom Africa, which is what delivers these.

    A plain JSON POST rather than an SDK — theirs is synchronous, and every
    send would block the event loop for the length of a round trip to Nairobi.

    Celcom answers 200 with a failure code in the body, so the status line
    alone is not enough to know whether a message left the building.
    """

    ENDPOINT = "https://isms.celcomafrica.com/api/services/sendsms/"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, phone: str, message: str) -> None:
        # Celcom wants a local number, not E.164 — everything else in this
        # service speaks E.164, so the conversion belongs here rather than
        # leaking a provider's format into the auth flow.
        local = phone.replace("+254", "0", 1) if phone.startswith("+254") else phone

        try:
            response = await self._client.post(
                self.ENDPOINT,
                json={
                    "apikey": settings.sms_api_key,
                    "partnerID": settings.sms_partner_id,
                    "shortcode": settings.sms_sender_id,
                    "mobile": local,
                    "message": message,
                },
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as error:
            log.error("sms_send_unreachable", phone=phone, error=str(error))
            return

        body = _describe(response)

        # 200 with a non-zero response code still means undelivered.
        if response.status_code >= 400 or body.get("failed"):
            # Logged, never raised. Whether the SMS left the building is our
            # problem; the student is told a code is coming either way, because
            # doing otherwise leaks which numbers have accounts.
            log.error(
                "sms_send_failed",
                phone=phone,
                status=response.status_code,
                detail=body.get("detail", "")[:300],
            )
        else:
            log.info("sms_sent", phone=phone)


def _describe(response: httpx.Response) -> dict:
    """
    Reads Celcom's answer without trusting its shape.

    Their gateway returns a list under ``responses`` on success and a bare
    object on some errors, so anything reaching in blindly raises on the day
    it matters most.
    """
    try:
        payload = response.json()
    except ValueError:
        return {"failed": True, "detail": response.text}

    entries = payload.get("responses") if isinstance(payload, dict) else None
    if isinstance(entries, list) and entries:
        first = entries[0]
        code = str(first.get("response-code", ""))
        # 200 is Celcom's own success code, distinct from the HTTP status.
        return {
            "failed": code not in {"200", "1000"},
            "detail": first.get("response-description", ""),
        }

    return {"failed": True, "detail": str(payload)[:300]}


def get_sms_provider(client: httpx.AsyncClient) -> SmsProvider:
    if settings.sms_configured:
        return CelcomSmsProvider(client)
    return ConsoleSmsProvider()
