# FDE Assessment: MCP and LLM Gateways

Runnable Python 3.11+ implementations for the four supplied tasks. The project uses the official `mcp` Python SDK, FastAPI, HTTPX, Pydantic v2, and on-disk SQLite.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Run each component in a separate terminal:

```bash
# Task 1: stdio MCP server (normally spawned by an MCP client)
.venv/bin/fde-mcp-server

# Task 2: MCP HTTP security gateway
.venv/bin/uvicorn fde_assessment.task2_mcp_gateway:app --port 8002

# Task 3: streaming PII guardrail
.venv/bin/uvicorn fde_assessment.task3_stream_guardrail:app --port 8003

# Task 4: rate limiting and fallback router
.venv/bin/uvicorn fde_assessment.task4_router:app --port 8004
```

Run the complete local end-to-end demonstration (no external API keys needed):

```bash
./scripts/run_demo.sh
```

The script starts disposable mock MCP and LLM upstreams, exercises all four
tasks, runs the complete test suite, and stops every process on exit.

## Task 1 — strict stdio MCP server

`get_customer_record` accepts exactly `CUST-` plus five digits. `trigger_refund` also requires a finite positive numeric amount and a reason containing at least ten non-whitespace characters. Unknown fields and string-coerced numbers are rejected.

Validation failures raise the SDK's protocol-level `MCPError` with JSON-RPC code `-32602` (`Invalid params`) and structured Pydantic error details. The SDK exclusively owns stdin/stdout. Python logging is configured to stderr, and the implementation contains no `print` calls.

Example MCP client configuration:

```json
{
  "mcpServers": {
    "customer-tools": {
      "command": "/absolute/path/to/.venv/bin/fde-mcp-server"
    }
  }
}
```

## Task 2 — MCP security gateway

The gateway listens on `POST /mcp`. It forwards the original JSON-RPC body for `tools/list` and authorized calls. Calls whose `params.name` begins with `admin_` are stopped locally unless the opaque bearer token resolves to role `admin`; the downstream is never contacted on denial.

Configuration:

```bash
export MCP_DOWNSTREAM_URL='http://127.0.0.1:9001/mcp'
export MCP_GATEWAY_TOKEN_ROLES='{"replace-me-admin":"admin","replace-me-viewer":"viewer"}'
```

The included `dev-*` defaults are only for local evaluation. Tokens are looked up server-side rather than decoding unsigned/unverified client claims. Invalid JSON, malformed calls, downstream failures, and RBAC denials use stable JSON-RPC error envelopes.

## Task 3 — streaming guardrail

The gateway accepts OpenAI-compatible requests at `POST /v1/chat/completions` and proxies an SSE response from `LLM_UPSTREAM_URL`. It incrementally parses UTF-8 and SSE frames, extracts `choices[0].delta.content`, and redacts:

- email addresses;
- US SSNs in `123-45-6789` form;
- 13–19 digit card candidates with optional spaces/hyphens that pass Luhn validation.

The redactor retains only the unfinished candidate token (bounded at 320 characters), so PII spanning arbitrary network chunks is caught without accumulating the complete response. Completed safe text is released as soon as a delimiter proves the token cannot grow further. Provider errors are sanitized.

```bash
export LLM_UPSTREAM_URL='https://provider.example/v1/chat/completions'
```

## Task 4 — SQLite limiter and model fallback

Requests to `POST /v1/chat/completions` require `X-Tenant-API-Key`. The limiter reserves estimated prompt tokens plus `max_tokens` in a rolling 60-second window. `BEGIN IMMEDIATE` makes check-and-insert atomic under concurrent requests; stale rows are evicted and WAL mode supports concurrent readers.

The primary has a hard 3-second HTTP timeout. A primary `429` or timeout triggers one backup attempt. Other upstream failures are returned through a standardized, sanitized gateway envelope and never expose response bodies, stack traces, provider URLs, or credentials.

```bash
export RATE_LIMIT_DB='./data/rate_limits.sqlite3'
export PRIMARY_LLM_URL='https://primary.example/v1/chat/completions'
export BACKUP_LLM_URL='https://backup.example/v1/chat/completions'
```

Token estimation is deliberately dependency-free (`ceil(serialized_prompt_chars / 4) + max_tokens`) and conservative. In production, inject the selected model's tokenizer and reconcile the reservation against the provider's final usage record.

## Test coverage

The tests exercise strict validation and MCP error codes, downstream non-invocation for forbidden tools, transparent forwarding, PII split across both text and byte/SSE boundaries, SQLite window eviction, atomic concurrent reservations, and fallback on both `429` and timeout.
