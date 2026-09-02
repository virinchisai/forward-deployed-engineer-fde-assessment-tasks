"""Small JSON-RPC 2.0 helpers shared by the gateway services."""

from __future__ import annotations

from typing import Any


def error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}

