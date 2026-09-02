import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fde_assessment.task2_mcp_gateway import TokenRoleResolver, create_app


@pytest.fixture
def downstream():
    app = FastAPI()
    app.state.calls = 0

    @app.post("/mcp")
    async def mcp(request: Request):
        app.state.calls += 1
        return JSONResponse(await request.json())

    return app


@pytest.mark.asyncio
async def test_viewer_cannot_call_admin_tool_and_request_is_not_forwarded(downstream):
    gateway = create_app(
        downstream_url="http://downstream/mcp",
        resolver=TokenRoleResolver({"viewer": "viewer", "admin": "admin"}),
        transport=httpx.ASGITransport(app=downstream),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post(
            "/mcp",
            headers={"authorization": "Bearer viewer"},
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "admin_reset_key"}},
        )
    assert response.json()["error"] == {"code": -32001, "message": "Unauthorized Tool Call"}
    assert downstream.state.calls == 0


@pytest.mark.asyncio
async def test_tools_list_and_admin_call_are_forwarded(downstream):
    gateway = create_app(
        downstream_url="http://downstream/mcp",
        resolver=TokenRoleResolver({"admin": "admin"}),
        transport=httpx.ASGITransport(app=downstream),
    )
    transport = httpx.ASGITransport(app=gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        listed = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = await client.post(
            "/mcp",
            headers={"authorization": "Bearer admin"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "admin_reset_key"}},
        )
    assert listed.json()["method"] == "tools/list"
    assert called.json()["params"]["name"] == "admin_reset_key"
    assert downstream.state.calls == 2

