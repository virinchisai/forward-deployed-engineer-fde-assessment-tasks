#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
venv_bin="$repo_dir/.venv/bin"
cd "$repo_dir"

if [[ ! -x "$venv_bin/python" ]]; then
  echo "missing .venv; run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

demo_dir="$(mktemp -d)"
pids=()

cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

start_service() {
  local name="$1"
  shift
  "$@" >"$demo_dir/$name.log" 2>&1 &
  pids+=("$!")
}

wait_for_url() {
  local url="$1"
  for _ in $(seq 1 50); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "service failed to start: $url" >&2
  return 1
}

start_service mock-mcp "$venv_bin/uvicorn" fde_assessment.demo_upstreams:mcp_app --port 9001
start_service mock-primary "$venv_bin/uvicorn" fde_assessment.demo_upstreams:primary_llm_app --port 9002
start_service mock-backup "$venv_bin/uvicorn" fde_assessment.demo_upstreams:backup_llm_app --port 9003

wait_for_url http://127.0.0.1:9001/health
wait_for_url http://127.0.0.1:9002/health
wait_for_url http://127.0.0.1:9003/health

start_service task2 env \
  MCP_DOWNSTREAM_URL=http://127.0.0.1:9001/mcp \
  MCP_GATEWAY_TOKEN_ROLES='{"demo-admin":"admin","demo-viewer":"viewer"}' \
  "$venv_bin/uvicorn" fde_assessment.task2_mcp_gateway:app --port 8002
start_service task3 env \
  LLM_UPSTREAM_URL=http://127.0.0.1:9002/v1/chat/completions \
  "$venv_bin/uvicorn" fde_assessment.task3_stream_guardrail:app --port 8003
start_service task4 env \
  PRIMARY_LLM_URL=http://127.0.0.1:9002/v1/chat/completions \
  BACKUP_LLM_URL=http://127.0.0.1:9003/v1/chat/completions \
  RATE_LIMIT_DB="$demo_dir/rate-limits.sqlite3" \
  "$venv_bin/uvicorn" fde_assessment.task4_router:app --port 8004

wait_for_url http://127.0.0.1:8002/openapi.json
wait_for_url http://127.0.0.1:8003/openapi.json
wait_for_url http://127.0.0.1:8004/openapi.json

echo '=== Task 1: MCP stdio transport ==='
"$venv_bin/python" scripts/demo_task1.py

echo
echo '=== Task 2: transparent tools/list ==='
curl -sS http://127.0.0.1:8002/mcp \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer demo-viewer' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
echo

echo '=== Task 2: viewer denied admin tool ==='
curl -sS http://127.0.0.1:8002/mcp \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer demo-viewer' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
echo

echo '=== Task 2: admin call forwarded ==='
curl -sS http://127.0.0.1:8002/mcp \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer demo-admin' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
echo

echo '=== Task 3: streaming PII redaction ==='
curl -sSN http://127.0.0.1:8003/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"demo","stream":true,"messages":[{"role":"user","content":"demo"}]}'
echo

echo '=== Task 4: primary success ==='
curl -sS http://127.0.0.1:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-tenant-api-key: demo-primary-tenant' \
  -d '{"model":"demo","messages":[{"role":"user","content":"hello"}],"max_tokens":100}'
echo

echo '=== Task 4: primary 429 causes backup fallback ==='
curl -sS http://127.0.0.1:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-tenant-api-key: demo-fallback-tenant' \
  -d '{"model":"force-429","messages":[{"role":"user","content":"hello"}],"max_tokens":100}'
echo

echo '=== Task 4: token rate limit ==='
curl -sS http://127.0.0.1:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-tenant-api-key: demo-limited-tenant' \
  -d '{"model":"demo","messages":[{"role":"user","content":"first"}],"max_tokens":32768}'
echo
curl -sS http://127.0.0.1:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-tenant-api-key: demo-limited-tenant' \
  -d '{"model":"demo","messages":[{"role":"user","content":"second"}],"max_tokens":32768}'
echo

echo '=== Full automated test suite ==='
"$venv_bin/pytest" -q
