"""Task 1: official-SDK MCP server over stdio with strict Pydantic validation."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import anyio
from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CustomerRecordInput(StrictModel):
    customer_id: str = Field(pattern=r"^CUST-[0-9]{5}$")


class RefundInput(CustomerRecordInput):
    amount: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_have_content(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("reason must contain at least 10 non-whitespace characters")
        return value


TOOLS = [
    Tool(
        name="get_customer_record",
        description="Return a customer record by canonical customer ID.",
        input_schema=CustomerRecordInput.model_json_schema(),
    ),
    Tool(
        name="trigger_refund",
        description="Trigger a positive-value refund for a customer.",
        input_schema=RefundInput.model_json_schema(),
    ),
]


def _validate(model: type[StrictModel], arguments: dict[str, Any] | None) -> StrictModel:
    try:
        return model.model_validate(arguments or {})
    except ValidationError as exc:
        # Pydantic's JSON form is structured and excludes input values, which
        # prevents rejected secrets or free text from being reflected on wire.
        raise MCPError(
            code=INVALID_PARAMS,
            message="Invalid tool arguments",
            data=json.loads(exc.json(include_input=False)),
        ) from exc


async def list_tools(
    _ctx: ServerRequestContext, _params: PaginatedRequestParams | None
) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def call_tool(
    _ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    if params.name == "get_customer_record":
        args = _validate(CustomerRecordInput, params.arguments)
        assert isinstance(args, CustomerRecordInput)
        result = {
            "customer_id": args.customer_id,
            "name": "Example Customer",
            "status": "active",
        }
    elif params.name == "trigger_refund":
        args = _validate(RefundInput, params.arguments)
        assert isinstance(args, RefundInput)
        result = {
            "refund_id": f"REF-{args.customer_id.removeprefix('CUST-')}",
            "customer_id": args.customer_id,
            "amount": args.amount,
            "status": "accepted",
        }
        logger.info("refund accepted customer_id=%s", args.customer_id)
    else:
        raise MCPError(
            code=INVALID_PARAMS,
            message="Unknown tool",
            data={"name": params.name},
        )

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structured_content=result,
    )


server = Server(
    "fde-customer-tools",
    version="0.1.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def _run() -> None:
    # The SDK owns stdin/stdout. All application logging is configured above
    # to use stderr; do not add print()/console output in this process.
    async with stdio_server() as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
