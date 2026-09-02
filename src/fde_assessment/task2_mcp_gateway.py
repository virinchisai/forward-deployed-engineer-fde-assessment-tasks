"""Task 2: HTTP JSON-RPC proxy with per-tool RBAC."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from .jsonrpc import error_response

UNAUTHORIZED_TOOL = -32001
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
PARSE_ERROR = -32700
BAD_GATEWAY = -32002
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    jsonrpc: str
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


class TokenRoleResolver:
    """Resolve opaque bearer tokens without trusting attacker-authored claims."""

    def __init__(self, token_roles: Mapping[str, str]) -> None:
        self._roles = dict(token_roles)

    def resolve(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        return self._roles.get(token)


def resolver_from_env() -> TokenRoleResolver:
    raw = os.getenv(
        "MCP_GATEWAY_TOKEN_ROLES",
        '{"dev-admin-token":"admin","dev-viewer-token":"viewer"}',
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and v in {"admin", "viewer"} for k, v in parsed.items()
    ):
        raise RuntimeError("MCP_GATEWAY_TOKEN_ROLES must map tokens to admin/viewer")
    return TokenRoleResolver(parsed)


def create_app(
    *,
    downstream_url: str | None = None,
    resolver: TokenRoleResolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(title="MCP Security Gateway")
    target = downstream_url or os.getenv("MCP_DOWNSTREAM_URL", "http://127.0.0.1:9001/mcp")
    role_resolver = resolver or resolver_from_env()

    @app.post("/mcp")
    async def proxy(request: Request) -> Response:
        try:
            raw = await request.body()
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(error_response(None, PARSE_ERROR, "Parse error"))
        try:
            payload = JsonRpcRequest.model_validate(decoded)
        except ValidationError:
            request_id = decoded.get("id") if isinstance(decoded, dict) else None
            return JSONResponse(error_response(request_id, INVALID_REQUEST, "Invalid Request"))

        if payload.jsonrpc != "2.0":
            return JSONResponse(
                error_response(payload.id, INVALID_REQUEST, "Invalid Request")
            )

        role = role_resolver.resolve(request.headers.get("authorization"))
        params = payload.params or {}
        tool_name = params.get("name")
        if payload.method == "tools/call" and not isinstance(tool_name, str):
            return JSONResponse(
                error_response(payload.id, INVALID_PARAMS, "Invalid tool call parameters")
            )
        if (
            payload.method == "tools/call"
            and isinstance(tool_name, str)
            and tool_name.startswith("admin_")
            and role != "admin"
        ):
            return JSONResponse(
                error_response(
                    payload.id,
                    UNAUTHORIZED_TOOL,
                    "Unauthorized Tool Call",
                )
            )

        # Forward bytes unchanged. Strip only hop-by-hop/framing headers; this
        # preserves MCP session/version headers used by downstream transports.
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}
        }
        try:
            async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
                upstream = await client.post(target, content=raw, headers=headers)
        except httpx.HTTPError:
            return JSONResponse(
                error_response(payload.id, BAD_GATEWAY, "Downstream MCP server unavailable")
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}
            },
        )

    return app


app = create_app()
