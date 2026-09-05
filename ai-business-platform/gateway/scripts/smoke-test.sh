#!/usr/bin/env bash
set -euo pipefail

gateway_dir=${GATEWAY_DIR:-/opt/ai-business-gateway}
cd "$gateway_dir"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

test_id="smoke-$(date -u +%Y%m%d%H%M%S)"
payload='{"action":"test.run","project_id":"worker-demo","environment":"research","parameters":{"operation":"self_test"},"limits":{"timeout_seconds":60}}'
public_base=http://127.0.0.1:8080
public_headers=()

if [[ ${CLOUDFLARE_ACCESS_REQUIRED:-false} == true ]]; then
  : "${CF_ACCESS_CLIENT_ID:?CF_ACCESS_CLIENT_ID is required}"
  : "${CF_ACCESS_CLIENT_SECRET:?CF_ACCESS_CLIENT_SECRET is required}"
  public_base=https://gateway.craftz.dev
  public_headers=(
    -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID"
    -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
  )

  edge_rejection=$(curl -sS -o /dev/null -w '%{http_code}' \
    -X POST "$public_base/v1/jobs" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $test_id-no-access" \
    --data "$payload")
  test "$edge_rejection" = 403
fi

unauthorized_code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "$public_base/v1/jobs" \
  "${public_headers[@]}" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id-unauthorized" \
  --data "$payload")
test "$unauthorized_code" = 401

response_one=$(curl -fsS -X POST "$public_base/v1/jobs" \
  "${public_headers[@]}" \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")
response_two=$(curl -fsS -X POST "$public_base/v1/jobs" \
  "${public_headers[@]}" \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")

job_one=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_one")
job_two=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_two")
test "$job_one" = "$job_two"

mismatch_code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "$public_base/v1/jobs" \
  "${public_headers[@]}" \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data '{"action":"code.build","project_id":"gateway-smoke","environment":"preview"}')
test "$mismatch_code" = 409

# A successful terminal state proves public authentication, Gateway dispatch,
# Worker authentication, execution, and the Tailnet callback path together.
state=QUEUED
job_response=
for _ in $(seq 1 60); do
  job_response=$(curl -fsS "$public_base/v1/jobs/$job_one" \
    "${public_headers[@]}" \
    -H "Authorization: Bearer $GATEWAY_API_TOKEN")
  state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$job_response")
  case "$state" in
    SUCCEEDED|FAILED_FINAL|CANCELLED) break ;;
  esac
  sleep 2
done
test "$state" = SUCCEEDED

worker_job_id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["worker_job_id"])' "$job_response")
occurred_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
event=$(printf '{"event_id":"%s-replay-check","gateway_job_id":"%s","dispatch_id":"gateway:%s:1","worker_job_id":"%s","event_type":"completed","sequence":4,"occurred_at":"%s","data":{"passed":true}}' \
  "$test_id" "$job_one" "$job_one" "$worker_job_id" "$occurred_at")

curl -fsS -o /dev/null -X POST http://127.0.0.1:8081/v1/worker-events \
  -H "Authorization: Bearer $WORKER_CALLBACK_TOKEN" \
  -H 'Content-Type: application/json' \
  --data "$event"

duplicate_response=$(curl -fsS -X POST http://127.0.0.1:8081/v1/worker-events \
  -H "Authorization: Bearer $WORKER_CALLBACK_TOKEN" \
  -H 'Content-Type: application/json' \
  --data "$event")
duplicate=$(python3 -c 'import json,sys; print(str(json.loads(sys.argv[1])["duplicate"]).lower())' "$duplicate_response")
test "$duplicate" = true

job_response=$(curl -fsS "$public_base/v1/jobs/$job_one" \
  "${public_headers[@]}" \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN")
state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$job_response")
test "$state" = SUCCEEDED

printf 'gateway smoke test passed: job_id=%s state=%s\n' "$job_one" "$state"
