"""Task 4: SQLite token limiter and timeout/429 model failover router."""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = Field(default=256, gt=0, le=32768)


def estimate_tokens(request: CompletionRequest) -> int:
    # Conservative dependency-free estimate: roughly four UTF-8 characters per
    # token plus the maximum output reservation. Production can swap in the
    # provider tokenizer without changing the limiter contract.
    prompt_chars = len(json.dumps(request.messages, separators=(",", ":")))
    return max(1, (prompt_chars + 3) // 4) + request.max_tokens


class SQLiteSlidingWindowLimiter:
    def __init__(self, db_path: str | Path, limit: int = 50_000, window_s: float = 60.0):
        self.db_path = str(db_path)
        self.limit = limit
        self.window_s = window_s

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS token_events ("
                "tenant TEXT NOT NULL, occurred_at REAL NOT NULL, tokens INTEGER NOT NULL)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_events_tenant_time "
                "ON token_events(tenant, occurred_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_events_time "
                "ON token_events(occurred_at)"
            )
            await db.commit()

    async def reserve(self, tenant: str, tokens: int, now: float | None = None) -> tuple[bool, int]:
        timestamp = time.time() if now is None else now
        cutoff = timestamp - self.window_s
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM token_events WHERE occurred_at <= ?", (cutoff,))
            cursor = await db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_events "
                "WHERE tenant = ? AND occurred_at > ?",
                (tenant, cutoff),
            )
            used = int((await cursor.fetchone())[0])
            if used + tokens > self.limit:
                # Commit stale-row eviction even when this reservation is denied.
                await db.commit()
                return False, max(0, self.limit - used)
            await db.execute(
                "INSERT INTO token_events(tenant, occurred_at, tokens) VALUES (?, ?, ?)",
                (tenant, timestamp, tokens),
            )
            await db.commit()
            return True, self.limit - used - tokens


@dataclass(frozen=True)
class RouterConfig:
    primary_url: str
    backup_url: str
    primary_timeout_s: float = 3.0


class ModelRouter:
    def __init__(
        self,
        config: RouterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    async def complete(self, body: bytes, authorization: str | None) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if authorization:
            headers["authorization"] = authorization
        async with httpx.AsyncClient(transport=self.transport) as client:
            try:
                primary = await client.post(
                    self.config.primary_url,
                    content=body,
                    headers=headers,
                    timeout=self.config.primary_timeout_s,
                )
                if primary.status_code != 429:
                    return primary
            except httpx.TimeoutException:
                pass
            return await client.post(
                self.config.backup_url,
                content=body,
                headers=headers,
                timeout=10.0,
            )


def gateway_error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(
    *,
    db_path: str | Path | None = None,
    config: RouterConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    database = Path(db_path or os.getenv("RATE_LIMIT_DB", "data/rate_limits.sqlite3"))
    database.parent.mkdir(parents=True, exist_ok=True)
    limiter = SQLiteSlidingWindowLimiter(database)
    router = ModelRouter(
        config
        or RouterConfig(
            primary_url=os.getenv("PRIMARY_LLM_URL", "http://127.0.0.1:9002/v1/chat/completions"),
            backup_url=os.getenv("BACKUP_LLM_URL", "http://127.0.0.1:9003/v1/chat/completions"),
        ),
        transport=transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await limiter.initialize()
        yield

    app = FastAPI(title="Resilient LLM Router", lifespan=lifespan)

    @app.post("/v1/chat/completions")
    async def completions(
        request: Request,
        x_tenant_api_key: str | None = Header(default=None),
    ) -> Response:
        if not x_tenant_api_key:
            return gateway_error("authentication_required", "Tenant API key required", 401)
        body = await request.body()
        try:
            completion = CompletionRequest.model_validate_json(body)
        except ValidationError:
            return gateway_error("invalid_request", "Invalid completion request", 400)
        allowed, remaining = await limiter.reserve(x_tenant_api_key, estimate_tokens(completion))
        if not allowed:
            response = gateway_error("rate_limit_exceeded", "Tenant token limit exceeded", 429)
            response.headers["x-ratelimit-remaining-tokens"] = str(remaining)
            return response
        try:
            upstream = await router.complete(body, request.headers.get("authorization"))
        except httpx.HTTPError:
            return gateway_error("providers_unavailable", "No model provider is available", 502)
        if upstream.status_code >= 400:
            return gateway_error("upstream_error", "Model provider could not complete request", 502)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers={"x-ratelimit-remaining-tokens": str(remaining)},
        )

    app.state.limiter = limiter
    app.state.router = router
    return app


app = create_app()
