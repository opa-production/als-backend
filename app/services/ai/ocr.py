"""
Reading a photograph of a page.

The scan path, which the plans have advertised as a Synapse feature and metered
through ``check_ocr`` since before it existed. This is the part that was
missing: ``allow_ocr_scans`` was sold, ``monthly_ocr_page_limit`` was counted
against, and nothing ever looked at an image.

Gemini by default, through its OpenAI-compatible endpoint — so the request
below is the same `/chat/completions` shape the tutor's adapter already speaks,
and moving OCR to OpenRouter, Groq or OpenAI is `OCR_BASE_URL` and `OCR_MODEL`
rather than a second adapter. The provider is not named anywhere in this file
for exactly that reason.

A vision model rather than a classical OCR engine (Tesseract and friends), and
the reason is what the input actually is. This is not a clean scan of printed
text — it is a phone photograph of handwriting, at an angle, in a lecture hall,
often with a diagram beside the words. Tesseract is excellent on the first thing
and close to useless on the rest, and it would also be a native binary to install
and keep alive on the box. A vision model reads handwriting, tolerates the angle,
and can say "this is a diagram of X" instead of returning nothing.

What comes back is prose, not a layout. That is deliberate: everything
downstream — chunking, retrieval, citation — works on text, and a bounding-box
format would have to be flattened into exactly this before it could be indexed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()

#: What the vision model can be handed.
#:
#: The intersection every provider agrees on, rather than any one provider's
#: full list — this file is meant to keep working when `OCR_BASE_URL` moves.
#:
#: HEIC is deliberately absent even though the scans bucket accepts it. iPhones
#: shoot HEIC by default and no vision API reads it, so it is refused *here*,
#: with an explanation a student can act on, rather than sent and failing as an
#: opaque provider error. Converting server-side would mean a native image
#: library on the box for a case the phone can avoid entirely.
READABLE_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})

#: The most a single photo may be, before base64.
#:
#: Below the plan file-size limits, which have already been applied at upload —
#: this is the separate question of what fits in one request. Base64 inflates by
#: a third, so 12 MB of image is 16 MB of JSON body.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

#: One photograph is one page. Stated rather than assumed because it is what the
#: OCR meter counts, and a multi-page scan arrives as several materials.
PAGES_PER_IMAGE = 1

#: Long enough for a densely written page, short enough that a model which has
#: started hallucinating a textbook is cut off rather than billed for.
MAX_TOKENS = 2000

#: Deterministic. This is transcription, not writing — there is one right answer
#: on the page and no reason to sample away from it.
TEMPERATURE = 0.0

_PROMPT = """You are transcribing a photograph of a student's course notes.

Write out everything on the page as plain text:

- Keep the original wording, spelling and technical terms exactly. Do not
  correct, summarise, translate or improve anything.
- Preserve headings, numbered lists and bullet points as they appear.
- For a diagram, chart or equation you cannot transcribe literally, write one
  short line in square brackets describing it, e.g. [diagram: labelled cross
  section of a leaf].
- If part of the page is cut off or illegible, write [illegible] there rather
  than guessing what it said.
- Output only the transcription. No preamble, no commentary, no markdown fences.

If the image contains no readable text at all, output exactly: NO_TEXT"""

#: What the model is told to say when there is nothing to read, and what this
#: module checks for. A sentinel rather than an empty response because "the model
#: returned nothing" and "the model looked and there was nothing there" are
#: different failures, and only the second is worth telling a student about.
_NO_TEXT = "NO_TEXT"


class OcrError(Exception):
    """
    A message meant for the student, not a stack trace.

    Raised for everything a person could plausibly fix — wrong format, an
    unreadable photo — and caught by the worker, which records it on the
    material so the app can show it.
    """


@dataclass(frozen=True)
class Scanned:
    text: str
    pages: int = PAGES_PER_IMAGE


def configured() -> bool:
    """
    Whether OCR can run at all.

    Checked before a material is claimed rather than after it is downloaded: a
    box with no vision key should leave scans queued for when one is set, not
    burn through them marking every one as failed.
    """
    return bool(settings.ocr_key and settings.ocr_base_url)


async def read_image(
    data: bytes, *, mime_type: str | None, client: httpx.AsyncClient
) -> Scanned:
    """
    One photograph to text.

    Every refusal here happens before the request is sent, because a rejected
    image should cost nothing — neither a token nor a second of the student's
    OCR allowance, which has already been checked by the time this is called.
    """
    if not configured():
        raise OcrError("Reading photos is not set up on our side yet.")

    kind = (mime_type or "").lower()
    if kind not in READABLE_MIME:
        if kind in ("image/heic", "image/heif"):
            raise OcrError(
                "That photo is in HEIC format, which we cannot read yet. On an "
                "iPhone, Settings → Camera → Formats → Most Compatible saves "
                "photos as JPEG."
            )
        raise OcrError("That file is not a photo we can read. Use a JPEG or PNG.")

    if len(data) > MAX_IMAGE_BYTES:
        raise OcrError(
            f"That photo is {len(data) / (1024 * 1024):.0f}MB, which is too "
            "large to read. Take it again at a lower resolution."
        )

    encoded = base64.b64encode(data).decode("ascii")

    payload = {
        "model": settings.ocr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{kind};base64,{encoded}"},
                    },
                ],
            }
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }

    try:
        response = await client.post(
            f"{settings.ocr_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.ocr_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            # Its own timeout, not the caller's. The shared worker client is set
            # up for Supabase downloads; a vision model reading a dense page of
            # handwriting routinely takes longer than that allows.
            timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=10.0),
        )
    except httpx.HTTPError as error:
        log.warning("ocr_unreachable", error=str(error))
        # Not an OcrError: nothing about this is the student's file. The worker
        # leaves the material queued rather than marking it failed, so it is
        # read when the provider comes back.
        raise

    if response.status_code >= 400:
        log.warning("ocr_failed", status=response.status_code, body=response.text[:400])
        if response.status_code == 429:
            # Rate limited. Also not the file's fault — requeue, do not fail.
            raise httpx.HTTPError("ocr rate limited")
        raise OcrError("We could not read that photo. Try again in a moment.")

    body = response.json()
    choices = body.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "").strip()

    if not text or text == _NO_TEXT:
        raise OcrError(
            "We could not find any text in that photo. Make sure the page fills "
            "the frame and the writing is in focus."
        )

    usage = body.get("usage") or {}
    log.info(
        "ocr_done",
        characters=len(text),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )

    return Scanned(text=text)
