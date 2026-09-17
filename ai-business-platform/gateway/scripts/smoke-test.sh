#!/usr/bin/env bash
set -euo pipefail

gateway_dir=${GATEWAY_DIR:-/opt/ai-business-gateway}
cd "$gateway_dir"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# ---------------------------------------------------------------------------
# 資格情報を curl の引数に載せない
#
# -H "Authorization: Bearer ..." や CF-Access-Client-Secret は、同じホストの
# 他プロセスから `ps` で読める。curl の設定をプロセス置換（/dev/fd）で渡せば
# 値は引数にもファイルにも現れない。printf は bash の組み込みなので、
# ここでも新しいプロセスは作られない。
# ---------------------------------------------------------------------------
access_headers() {
  if [[ ${CLOUDFLARE_ACCESS_REQUIRED:-false} == true ]]; then
    printf 'header = "CF-Access-Client-Id: %s"\n' "$CF_ACCESS_CLIENT_ID"
    printf 'header = "CF-Access-Client-Secret: %s"\n' "$CF_ACCESS_CLIENT_SECRET"
  fi
}

curl_access_only() {
  curl --config <(access_headers) "$@"
}

curl_auth() {
  local token=$1
  shift
  curl --config <(
    printf 'header = "Authorization: Bearer %s"\n' "$token"
    access_headers
  ) "$@"
}

test_id="smoke-$(date -u +%Y%m%d%H%M%S)"
payload='{"action":"test.run","project_id":"worker-demo","environment":"research","parameters":{"operation":"self_test"},"limits":{"timeout_seconds":60}}'
public_base=http://127.0.0.1:8080

if [[ ${CLOUDFLARE_ACCESS_REQUIRED:-false} == true ]]; then
  : "${CF_ACCESS_CLIENT_ID:?CF_ACCESS_CLIENT_ID is required}"
  : "${CF_ACCESS_CLIENT_SECRET:?CF_ACCESS_CLIENT_SECRET is required}"
  public_base=https://gateway.craftz.dev

  edge_rejection=$(curl_access_only -sS -o /dev/null -w '%{http_code}' \
    -X POST "$public_base/v1/jobs" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $test_id-no-access" \
    --data "$payload")
  test "$edge_rejection" = 403
fi

unauthorized_code=$(curl_access_only -sS -o /dev/null -w '%{http_code}' \
  -X POST "$public_base/v1/jobs" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id-unauthorized" \
  --data "$payload")
test "$unauthorized_code" = 401

response_one=$(curl_auth "$GATEWAY_API_TOKEN" -fsS -X POST "$public_base/v1/jobs" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")
response_two=$(curl_auth "$GATEWAY_API_TOKEN" -fsS -X POST "$public_base/v1/jobs" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data "$payload")

job_one=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_one")
job_two=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["job_id"])' "$response_two")
test "$job_one" = "$job_two"

mismatch_code=$(curl_auth "$GATEWAY_API_TOKEN" -sS -o /dev/null -w '%{http_code}' \
  -X POST "$public_base/v1/jobs" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $test_id" \
  --data '{"action":"code.build","project_id":"gateway-smoke","environment":"preview"}')
test "$mismatch_code" = 409

# A successful terminal state proves public authentication, Gateway dispatch,
# Worker authentication, execution, and the Tailnet callback path together.
state=QUEUED
job_response=
for _ in $(seq 1 60); do
  job_response=$(curl_auth "$GATEWAY_API_TOKEN" -fsS "$public_base/v1/jobs/$job_one")
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

# A new event must not rewrite a terminal job's approved evidence.
# Exact callback replay idempotency is covered by the DB integration suite.
terminal_rewrite_code=$(curl_auth "$WORKER_CALLBACK_TOKEN" -sS -o /dev/null \
  -w '%{http_code}' -X POST http://127.0.0.1:8081/v1/worker-events \
  -H 'Content-Type: application/json' \
  --data "$event")
test "$terminal_rewrite_code" = 409

job_response=$(curl_auth "$GATEWAY_API_TOKEN" -fsS "$public_base/v1/jobs/$job_one")
state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$job_response")
test "$state" = SUCCEEDED

printf 'gateway smoke test passed: job_id=%s state=%s\n' "$job_one" "$state"
