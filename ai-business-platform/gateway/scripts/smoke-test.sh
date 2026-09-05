#!/usr/bin/env bash
set -euo pipefail

gateway_dir=${GATEWAY_DIR:-/opt/ai-business-gateway}
cd "$gateway_dir"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

test_id="smoke-$(date -u +%Y%m%d%H%M%S)"
payload='{"action":"test.run","project_id":"gateway-smoke","environment":"preview","parameters":{"suite":"deployment"},"limits":{"timeout_seconds":300}}'

unauthorized_code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST http://127.0.0.1:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id-unauthorized" \
  --data "$payload")
test "$unauthorized_code" = 401

response_one=$(curl -fsS -X POST http://127.0.0.1:8080/v1/jobs \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")
response_two=$(curl -fsS -X POST http://127.0.0.1:8080/v1/jobs \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")

job_one=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_one")
job_two=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_two")
test "$job_one" = "$job_two"

mismatch_code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST http://127.0.0.1:8080/v1/jobs \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data '{"action":"code.build","project_id":"gateway-smoke","environment":"preview"}')
test "$mismatch_code" = 409

occurred_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
event=$(printf '{"event_id":"%s-completed","gateway_job_id":"%s","dispatch_id":"%s-dispatch","worker_job_id":"%s-worker","event_type":"completed","sequence":1,"occurred_at":"%s","data":{"passed":true}}' \
  "$test_id" "$job_one" "$test_id" "$test_id" "$occurred_at")

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

job_response=$(curl -fsS "http://127.0.0.1:8080/v1/jobs/$job_one" \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN")
state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$job_response")
test "$state" = SUCCEEDED

printf 'gateway smoke test passed: job_id=%s state=%s\n' "$job_one" "$state"
