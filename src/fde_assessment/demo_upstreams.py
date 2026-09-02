"""Local-only mock upstreams used by the end-to-end demonstration script."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


mcp_app = FastAPI(title="Demo downstream MCP server")
primary_llm_app = FastAPI(title="Demo primary LLM provider")
backup_llm_app = FastAPI(title="Demo backup LLM provider")


@mcp_app.get("/health")
async def mcp_health() -> dict[str, bool]:
    return {"ok": True}


@mcp_app.post("/mcp")
async def downstream_mcp(request: Request) -> JSONResponse:
    payload = await request.json()
    request_id = payload.get("id")
    if payload.get("method") == "tools/list":
        result = {
            "tools": [
                {"name": "read_status", "inputSchema": {"type": "object"}},
                {"name": "admin_reset_key", "inputSchema": {"type": "object"}},
            ],
            "downstream_called": True,
        }
    else:
        result = {
            "content": [{"type": "text", "text": "mock tool executed"}],
            "downstream_called": True,
        }
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


@primary_llm_app.get("/health")
async def primary_health() -> dict[str, bool]:
    return {"ok": True}


@primary_llm_app.post("/v1/chat/completions")
async def primary_completion(request: Request):
    payload = await request.json()
    if payload.get("model") == "force-429":
        return JSONResponse({"error": "demo primary capacity"}, status_code=429)
    if payload.get("stream"):
        parts = [
            "Contact alice@exa",
            "mple.com, SSN 123-45-",
            "6789, card 4242 4242 ",
            "4242 4242.",
        ]

        async def events() -> AsyncIterator[bytes]:
            for part in parts:
                event = {"choices": [{"delta": {"content": part}}]}
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                await asyncio.sleep(0.01)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": "demo-primary",
            "provider": "primary",
            "choices": [{"message": {"role": "assistant", "content": "primary response"}}],
        }
    )


@backup_llm_app.get("/health")
async def backup_health() -> dict[str, bool]:
    return {"ok": True}


@backup_llm_app.post("/v1/chat/completions")
async def backup_completion(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "id": "demo-backup",
            "provider": "backup",
            "choices": [{"message": {"role": "assistant", "content": "fallback response"}}],
        }
    )

