import json
import sys

import pytest
from mcp import Client, MCPError
from mcp.client.stdio import StdioServerParameters
from mcp.types import INVALID_PARAMS, CallToolRequestParams

from fde_assessment.task1_mcp_server import (
    CustomerRecordInput,
    RefundInput,
    _validate,
    call_tool,
)


def test_customer_id_is_strictly_validated():
    assert _validate(CustomerRecordInput, {"customer_id": "CUST-12345"}).customer_id == "CUST-12345"
    for invalid in ("CUST-1234", "cust-12345", "CUST-ABCDE", 12345):
        with pytest.raises(MCPError) as caught:
            _validate(CustomerRecordInput, {"customer_id": invalid})
        assert caught.value.error.code == INVALID_PARAMS


def test_refund_rejects_bad_values_and_extra_fields():
    bad_inputs = [
        {"customer_id": "CUST-12345", "amount": 0.0, "reason": "long enough reason"},
        {"customer_id": "CUST-12345", "amount": "12.5", "reason": "long enough reason"},
        {"customer_id": "CUST-12345", "amount": 12.5, "reason": "short"},
        {"customer_id": "CUST-12345", "amount": 12.5, "reason": "          "},
        {
            "customer_id": "CUST-12345",
            "amount": 12.5,
            "reason": "long enough reason",
            "unexpected": True,
        },
    ]
    for value in bad_inputs:
        with pytest.raises(MCPError):
            _validate(RefundInput, value)


@pytest.mark.asyncio
async def test_successful_call_returns_structured_result():
    result = await call_tool(
        None,
        CallToolRequestParams(
            name="get_customer_record", arguments={"customer_id": "CUST-12345"}
        ),
    )
    assert json.loads(result.content[0].text)["status"] == "active"


@pytest.mark.asyncio
async def test_real_stdio_transport_and_protocol_error():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "fde_assessment.task1_mcp_server"]
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == [
            "get_customer_record",
            "trigger_refund",
        ]
        result = await client.call_tool(
            "get_customer_record", {"customer_id": "CUST-12345"}
        )
        assert result.structured_content["customer_id"] == "CUST-12345"
        with pytest.raises(MCPError) as caught:
            await client.call_tool("get_customer_record", {"customer_id": "bad"})
        assert caught.value.error.code == INVALID_PARAMS
