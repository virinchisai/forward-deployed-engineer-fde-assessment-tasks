import asyncio

import httpx
import pytest

from fde_assessment.task4_router import ModelRouter, RouterConfig, SQLiteSlidingWindowLimiter


@pytest.mark.asyncio
async def test_sliding_window_is_atomic_under_concurrency(tmp_path):
    limiter = SQLiteSlidingWindowLimiter(tmp_path / "limits.sqlite3", limit=100, window_s=60)
    await limiter.initialize()
    results = await asyncio.gather(*(limiter.reserve("tenant-a", 30, now=1000) for _ in range(5)))
    assert sum(allowed for allowed, _remaining in results) == 3


@pytest.mark.asyncio
async def test_expired_entries_are_evicted(tmp_path):
    limiter = SQLiteSlidingWindowLimiter(tmp_path / "limits.sqlite3", limit=100, window_s=60)
    await limiter.initialize()
    assert (await limiter.reserve("tenant", 100, now=1000))[0]
    assert not (await limiter.reserve("tenant", 1, now=1059))[0]
    assert (await limiter.reserve("tenant", 100, now=1061))[0]


@pytest.mark.asyncio
async def test_router_falls_back_only_on_primary_429():
    calls = []

    async def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/primary":
            return httpx.Response(429)
        return httpx.Response(200, json={"provider": "backup"})

    router = ModelRouter(
        RouterConfig("http://provider/primary", "http://provider/backup"),
        transport=httpx.MockTransport(handler),
    )
    response = await router.complete(b"{}", None)
    assert response.json() == {"provider": "backup"}
    assert calls == ["/primary", "/backup"]


@pytest.mark.asyncio
async def test_router_falls_back_on_timeout():
    async def handler(request: httpx.Request):
        if request.url.path == "/primary":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"provider": "backup"})

    router = ModelRouter(
        RouterConfig("http://provider/primary", "http://provider/backup"),
        transport=httpx.MockTransport(handler),
    )
    assert (await router.complete(b"{}", None)).status_code == 200

