"""Task 3: low-latency, bounded-memory PII redaction for SSE LLM streams."""

from __future__ import annotations

import codecs
import json
import os
import re
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
EMAIL_SUFFIX_RE = re.compile(r"[\w.+-]+(?:@[a-zA-Z0-9.-]*)?$")
NUMBER_SUFFIX_RE = re.compile(r"\d[\d -]*$")


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub("[REDACTED]", text)
    text = SSN_RE.sub("[REDACTED]", text)
    return CARD_RE.sub(
        lambda match: "[REDACTED]" if _luhn(match.group(0)) else match.group(0),
        text,
    )


class StreamingRedactor:
    """Hold only an unfinished PII-capable token across provider chunks."""

    def __init__(self, max_token_chars: int = 320) -> None:
        self._pending = ""
        self._max_token_chars = max_token_chars

    def feed(self, text: str) -> str:
        self._pending += text
        # Redact complete matches before choosing the unfinished suffix. This
        # matters when punctuation immediately follows a card or email.
        self._pending = redact_pii(self._pending)
        # Retain the shortest suffix that could still grow into supported PII.
        # This is independent of provider chunk boundaries and, unlike simply
        # retaining the final N characters, usually delays only the current word.
        starts = [
            match.start()
            for pattern in (EMAIL_SUFFIX_RE, NUMBER_SUFFIX_RE)
            if (match := pattern.search(self._pending)) is not None
        ]
        safe_end = min(starts, default=len(self._pending))
        if safe_end:
            ready, self._pending = self._pending[:safe_end], self._pending[safe_end:]
            return redact_pii(ready)
        # Bound adversarial/unbroken upstream output. Keep enough suffix for all
        # supported patterns while releasing the older prefix.
        if len(self._pending) > self._max_token_chars:
            cut = len(self._pending) - self._max_token_chars
            ready, self._pending = self._pending[:cut], self._pending[cut:]
            return redact_pii(ready)
        return ""

    def flush(self) -> str:
        ready, self._pending = self._pending, ""
        return redact_pii(ready)


async def _sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    async for chunk in chunks:
        pending += decoder.decode(chunk)
        while True:
            separators = [(pending.find(sep), sep) for sep in ("\n\n", "\r\n\r\n")]
            separators = [(index, sep) for index, sep in separators if index >= 0]
            if not separators:
                break
            index, separator = min(separators, key=lambda item: item[0])
            event, pending = pending[:index], pending[index + len(separator) :]
            yield event
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


async def redact_openai_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    redactor = StreamingRedactor()
    async for event in _sse_events(chunks):
        lines = event.splitlines()
        data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if not data_lines:
            yield (event + "\n\n").encode()
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            tail = redactor.flush()
            if tail:
                yield _content_event(tail)
            yield b"data: [DONE]\n\n"
            continue
        try:
            parsed = json.loads(data)
            content = parsed["choices"][0]["delta"].get("content")
        except json.JSONDecodeError:
            # Fail closed: never pass an unparseable provider data frame through
            # the guardrail, because its content cannot be classified safely.
            continue
        except (KeyError, IndexError, TypeError):
            # Preserve valid provider metadata events that contain no text delta.
            yield (event + "\n\n").encode()
            continue
        if not isinstance(content, str):
            yield (event + "\n\n").encode()
            continue
        safe = redactor.feed(content)
        if safe:
            parsed["choices"][0]["delta"]["content"] = safe
            yield ("data: " + json.dumps(parsed, separators=(",", ":")) + "\n\n").encode()
    tail = redactor.flush()
    if tail:
        yield _content_event(tail)


def _content_event(text: str) -> bytes:
    payload = {"choices": [{"delta": {"content": text}}]}
    return ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()


def create_app(
    *,
    upstream_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(title="LLM Streaming Guardrail")
    target = upstream_url or os.getenv(
        "LLM_UPSTREAM_URL", "http://127.0.0.1:9002/v1/chat/completions"
    )

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.body()
        client = httpx.AsyncClient(transport=transport, timeout=None)
        try:
            upstream_request = client.build_request(
                "POST",
                target,
                content=body,
                headers={
                    "content-type": request.headers.get("content-type", "application/json"),
                    **(
                        {"authorization": request.headers["authorization"]}
                        if "authorization" in request.headers
                        else {}
                    ),
                },
            )
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse(
                {"error": {"code": "upstream_unavailable", "message": "LLM provider unavailable"}},
                status_code=502,
            )

        if upstream.status_code >= 400:
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(
                {"error": {"code": "upstream_error", "message": "LLM provider rejected the request"}},
                status_code=502,
            )

        async def guarded_stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in redact_openai_sse(upstream.aiter_bytes()):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(guarded_stream(), media_type="text/event-stream")

    return app


app = create_app()
