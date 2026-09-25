#!/usr/bin/env bash
# Live RBAC replay against a throwaway app instance on the TEST DB (port 8099).
set -uo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python

# ── Test env (mirrors tests/conftest.py; isolated DB + redis db13) ──
export APP_ENV=test
export APP_SECRET_KEY="test-secret-key-must-be-at-least-32-chars-long!!"
export APP_BASE_URL="http://127.0.0.1:8099"
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/fluxito_test"
export REDIS_URL="redis://localhost:6379/13"
export TOKEN_ENCRYPTION_KEY="$($PY -c 'import base64;print(base64.urlsafe_b64encode(b"0"*32).decode())')"
export GOOGLE_IDENTITY_REDIRECT_URI="http://127.0.0.1:8099/auth/google/identity/callback"
export GOOGLE_DATA_REDIRECT_URI="http://127.0.0.1:8099/auth/google/data/callback"
export GOOGLE_SIGNIN_REDIRECT_URI="http://127.0.0.1:8099/auth/google/signin/callback"
export MCP_ALLOWED_REDIRECT_URIS="https://claude.ai/api/mcp/auth_callback,http://127.0.0.1:8099/callback"

echo "── Seeding test DB + redis(db13) ──"
$PY tests/_live_rbac/seed.py || { echo "seed failed"; exit 1; }

echo "── Starting uvicorn on :8099 ──"
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --log-level warning \
    > tests/_live_rbac/uvicorn.log 2>&1 &
UVPID=$!
cleanup() { kill $UVPID 2>/dev/null; wait $UVPID 2>/dev/null; }
trap cleanup EXIT

echo "── Waiting for readiness ──"
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8099/mcp \
         -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null)
  if [ "$code" = "401" ]; then echo "  app up (401 on unauth /mcp after ${i} tries)"; break; fi
  if ! kill -0 $UVPID 2>/dev/null; then echo "  uvicorn died:"; tail -20 tests/_live_rbac/uvicorn.log; exit 1; fi
  sleep 0.5
done

echo "── Running MCP client replay ──"
$PY tests/_live_rbac/replay.py
RC=$?
echo "── uvicorn tail ──"; tail -5 tests/_live_rbac/uvicorn.log
exit $RC
