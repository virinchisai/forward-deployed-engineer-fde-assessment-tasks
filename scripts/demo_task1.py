"""Exercise Task 1 through the real SDK stdio client transport."""

from __future__ import annotations

import json
import sys

import anyio
from mcp import Client, MCPError
from mcp.client.stdio import StdioServerParameters


async def run() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fde_assessment.task1_mcp_server"],
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        print("tools:", [tool.name for tool in tools.tools])
        customer = await client.call_tool(
            "get_customer_record", {"customer_id": "CUST-12345"}
        )
        print("customer:", json.dumps(customer.structured_content, sort_keys=True))
        refund = await client.call_tool(
            "trigger_refund",
            {
                "customer_id": "CUST-12345",
                "amount": 25.5,
                "reason": "Duplicate payment refund",
            },
        )
        print("refund:", json.dumps(refund.structured_content, sort_keys=True))
        try:
            await client.call_tool("get_customer_record", {"customer_id": "bad"})
        except MCPError as error:
            print("invalid-input-error:", error.error.code, error.error.message)


if __name__ == "__main__":
    anyio.run(run)

