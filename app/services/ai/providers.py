"""
Talking to the models.

One adapter per wire format, and a registry of models on top. The point of the
split is that **adding a model is data, not code** as long as it speaks a format
already implemented — DeepSeek and OpenAI share one, so the second costs an
entry in `MODELS` and a key in the environment.

Why not LangChain, given it exists for exactly this: it does not import on the
development machine. `langgraph` pulls `xxhash` and `langchain-openai` pulls
`tiktoken`, and both ship unsigned compiled extensions that this Windows box's
Application Control policy blocks outright. Code nobody here can run or test is
worse than fifty lines of httpx, so the seam is kept narrow — `Provider` below
is a two-method protocol, and swapping a LangChain chat model in behind it later
is a self-contained change.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx
import structlog

from app.core.config import settings
from app.core.errors import AppError

log = structlog.get_logger()


# --- What a model exchange looks like ----------------------------------------


@dataclass(frozen=True)
class Message:
    #: system | user | assistant
    role: str
    content: str


@dataclass
class Usage:
    """
    What the exchange cost.

    Kept even when a provider does not report it — zeros are honest, and the
    admin console's token figures are visibly zero rather than quietly wrong.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ModelSpec:
    id: str
    provider: str
    label: str
    #: One line, shown under the name in the model picker.
    description: str
    #: False until the provider has a key. Listed either way.
    available: bool = False
    #: Why it is unavailable, when it is.
    note: str = ""
    #: Whether this model's adapter is written at all, as opposed to merely
    #: lacking a key. The two are different problems and the console should not
    #: conflate them.
    implemented: bool = True
    tags: list[str] = field(default_factory=list)


class Provider(Protocol):
    """
    The whole surface a model needs to expose.

    Two methods rather than one: streaming is what a student sees, and a
    single-shot completion is what the classifier and the quiz builder need,
    where a stream would just be reassembled anyway.
    """

    async def stream(
        self, messages: list[Message], *, model: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]: ...

    async def complete(
        self, messages: list[Message], *, model: str, max_tokens: int, temperature: float
    ) -> tuple[str, Usage]: ...


# --- OpenAI-compatible -------------------------------------------------------


class OpenAICompatible:
    """
    Serves DeepSeek today and OpenAI unchanged.

    Both speak the same `/chat/completions` request and the same SSE response,
    so one adapter covers them and the difference is a base URL and a key.
    """

    def __init__(self, *, base_url: str, api_key: str, name: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._name = name

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            # Without this the streamed response carries no usage at all and
            # every answer is recorded as costing nothing.
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def stream(
        self, messages: list[Message], *, model: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        """
        Yields text as it arrives.

        Its own client, not the shared one on app state: that is configured with
        a 15 second timeout for Kora and Supabase, and a model writing 900
        tokens routinely takes longer. Sharing it would cut answers off
        mid-sentence and look like the tutor failing at random.
        """
        timeout = httpx.Timeout(
            settings.ai_timeout_seconds,
            # A model can think for a while before the first token. What must
            # not stall is the *connection*, so that stays short.
            connect=10.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=self._payload(messages, model, max_tokens, temperature, True),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")[:400]
                        log.warning(
                            "ai_stream_failed",
                            provider=self._name,
                            model=model,
                            status=response.status_code,
                            body=body,
                        )
                        raise AppError(_message_for(response.status_code, self._name))

                    async for line in response.aiter_lines():
                        for piece in _parse_sse_line(line):
                            yield piece

            except httpx.HTTPError as error:
                log.warning("ai_stream_unreachable", provider=self._name, error=str(error))
                raise AppError(
                    "The tutor could not be reached. Try again in a moment."
                ) from None

    async def complete(
        self, messages: list[Message], *, model: str, max_tokens: int, temperature: float
    ) -> tuple[str, Usage]:
        timeout = httpx.Timeout(settings.ai_timeout_seconds, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=self._payload(messages, model, max_tokens, temperature, False),
                )
            except httpx.HTTPError as error:
                log.warning("ai_complete_unreachable", provider=self._name, error=str(error))
                raise AppError("The tutor could not be reached.") from None

            if response.status_code >= 400:
                log.warning(
                    "ai_complete_failed",
                    provider=self._name,
                    status=response.status_code,
                    body=response.text[:400],
                )
                raise AppError(_message_for(response.status_code, self._name))

            body = response.json()

        choices = body.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}

        return text, Usage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


def _parse_sse_line(line: str) -> list[str]:
    """
    One SSE line to zero or more pieces of text.

    Returns a list rather than yielding so the caller's loop stays flat, and so
    a malformed line is simply nothing rather than an exception halfway through
    a student's answer.
    """
    if not line or not line.startswith("data:"):
        return []

    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return []

    try:
        event = json.loads(data)
    except ValueError:
        # A partial or non-JSON keepalive. Dropping it is right: there is
        # nothing to show and nothing to fix.
        return []

    out: list[str] = []
    for choice in event.get("choices") or []:
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            out.append(content)

    return out


def _message_for(status: int, provider: str) -> str:
    """
    A provider failure in words a student can act on.

    The status is not shown. "Tutor unavailable (429)" tells a student nothing
    and tells an attacker which key is rate-limited; the log has the detail.
    """
    if status in (401, 403):
        return "The tutor is not configured correctly on our side."
    if status == 429:
        return "The tutor is busy right now. Try again in a moment."
    if status in (402,):
        return "The tutor is temporarily unavailable."
    return "The tutor could not answer that. Try again in a moment."


# --- Not yet written ---------------------------------------------------------


class NotImplementedProvider:
    """
    A placeholder that fails honestly.

    Anthropic and Gemini do not speak the OpenAI format — different message
    shapes, different streaming events, a separate system parameter. Pretending
    otherwise by pointing them at `OpenAICompatible` would produce a model that
    is selectable and then fails with a parse error, which is worse than one
    that is plainly listed as unavailable.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    async def stream(self, *_args, **_kwargs) -> AsyncIterator[str]:
        raise AppError(f"{self._name} is not available yet.")
        yield ""  # pragma: no cover — makes this an async generator

    async def complete(self, *_args, **_kwargs) -> tuple[str, Usage]:
        raise AppError(f"{self._name} is not available yet.")


# --- The catalogue -----------------------------------------------------------

DEEPSEEK_BASE = "https://api.deepseek.com"
OPENAI_BASE = "https://api.openai.com/v1"


def _specs() -> list[ModelSpec]:
    """
    Every model the product means to offer, whether or not it works today.

    Rebuilt on each call rather than computed once at import, because
    `available` depends on settings that a test may monkeypatch.
    """
    deepseek = bool(settings.deepseek_api_key)
    openai = bool(settings.openai_api_key)

    return [
        ModelSpec(
            id="deepseek-chat",
            provider="deepseek",
            label="DeepSeek",
            description="Fast and steady. Good for explanations and revision.",
            available=deepseek,
            note="" if deepseek else "DEEPSEEK_API_KEY is not set.",
            tags=["default"],
        ),
        ModelSpec(
            id="deepseek-reasoner",
            provider="deepseek",
            label="DeepSeek Reasoner",
            description="Thinks longer. Better on multi-step problems, slower to answer.",
            available=deepseek,
            note="" if deepseek else "DEEPSEEK_API_KEY is not set.",
            tags=["reasoning"],
        ),
        ModelSpec(
            id="gpt-4.1-mini",
            provider="openai",
            label="ChatGPT",
            description="OpenAI's general-purpose model.",
            available=openai,
            note="" if openai else "OPENAI_API_KEY is not set.",
        ),
        # Below here the adapter itself is missing, not just the key. Said
        # separately so the console can show a different reason.
        ModelSpec(
            id="claude-sonnet-5",
            provider="anthropic",
            label="Claude",
            description="Strong on long explanations and careful reading.",
            available=False,
            implemented=False,
            note="The Anthropic adapter is not written yet.",
        ),
        ModelSpec(
            id="gemini-2.5-flash",
            provider="google",
            label="Gemini",
            description="Fast, and handles very long documents.",
            available=False,
            implemented=False,
            note="The Gemini adapter is not written yet.",
        ),
    ]


def catalogue() -> list[ModelSpec]:
    return _specs()


def spec_for(model_id: str) -> ModelSpec | None:
    return next((spec for spec in _specs() if spec.id == model_id), None)


def resolve(model_id: str | None) -> ModelSpec:
    """
    Which model to actually use.

    An unknown or unavailable choice falls back to whatever *is* available
    rather than failing, because a student who picked Claude in the app before
    the key existed should still get an answer. The response says which model
    replied, so the substitution is visible rather than silent.
    """
    requested = spec_for(model_id) if model_id else None

    if requested and requested.available:
        return requested

    default = spec_for(settings.ai_default_model)
    if default and default.available:
        return default

    fallback = next((spec for spec in _specs() if spec.available), None)
    if fallback:
        return fallback

    raise AppError("The tutor is not configured on this server yet.")


def provider_for(spec: ModelSpec) -> Provider:
    if spec.provider == "deepseek":
        return OpenAICompatible(
            base_url=DEEPSEEK_BASE, api_key=settings.deepseek_api_key, name="deepseek"
        )
    if spec.provider == "openai":
        return OpenAICompatible(
            base_url=OPENAI_BASE, api_key=settings.openai_api_key, name="openai"
        )
    return NotImplementedProvider(spec.label)
