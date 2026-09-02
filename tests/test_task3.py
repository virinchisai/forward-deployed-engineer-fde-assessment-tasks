import json

import pytest

from fde_assessment.task3_stream_guardrail import StreamingRedactor, redact_openai_sse


def test_redactor_handles_pii_split_across_chunks():
    redactor = StreamingRedactor()
    output = redactor.feed("Email alice@exa")
    output += redactor.feed("mple.com, SSN 123-45-")
    output += redactor.feed("6789 and card 4242 4242 ")
    output += redactor.feed("4242 4242.")
    output += redactor.flush()
    assert output == "Email [REDACTED], SSN [REDACTED] and card [REDACTED]."


@pytest.mark.asyncio
async def test_sse_payloads_stay_valid_and_redacted():
    raw = (
        b'data: {"choices":[{"delta":{"content":"mail bob@exa"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"mple.com done"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def chunks():
        for offset in range(0, len(raw), 13):
            yield raw[offset : offset + 13]

    output = b"".join([chunk async for chunk in redact_openai_sse(chunks())]).decode()
    assert "bob@example.com" not in output
    assert "[REDACTED]" in output
    for event in output.strip().split("\n\n"):
        data = event.removeprefix("data: ")
        if data != "[DONE]":
            json.loads(data)

