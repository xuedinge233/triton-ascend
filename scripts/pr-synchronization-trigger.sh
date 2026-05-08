#!/bin/bash
# Blue/Yellow cross-region pipeline trigger (CI version).
# Flow: POST /start -> poll /query -> exit on terminal status (SUCCESS=0, otherwise 1).
#
# Required env vars:
#   BASE_URL, APP_CODE, APP_KEY, APP_SECRET
#   YELLOW_PIPELINE_ID, YELLOW_GROUP_ID
#   PR_ID, PR_TITLE, PR_DESC
#   URL_B, BRANCH_B, URL_Y, BRANCH_Y
# Optional env vars:
#   POLL_WAIT  (default 15)
#   MAX_POLL   (default 240)
#   STATUS_FILE (write latest status / msg for the workflow PR comment step)
set -euo pipefail

: "${BASE_URL:?}"; : "${APP_CODE:?}"; : "${APP_KEY:?}"; : "${APP_SECRET:?}"
: "${YELLOW_PIPELINE_ID:?}"; : "${YELLOW_GROUP_ID:?}"
: "${PR_ID:?}"; : "${PR_TITLE:?}";
: "${URL_B:?}"; : "${BRANCH_B:?}"; : "${URL_Y:?}"; : "${BRANCH_Y:?}"
PR_DESC="${PR_DESC:-}"

POLL_WAIT="${POLL_WAIT:-15}"
MAX_POLL="${MAX_POLL:-240}"
STATUS_FILE="${STATUS_FILE:-}"

H_CODE="X-Apig-AppCode: ${APP_CODE}"
H_KEY="AppKey: ${APP_KEY}"
H_SEC="AppSecret: ${APP_SECRET}"
H_JSON="Content-Type: application/json"

TS=$(date +%Y%m%d%H%M%S)
BRI="pr-${PR_ID}-${TS}"
BTI="task-${PR_ID}-${TS}"

log(){ echo "[$(date '+%H:%M:%S')] $*"; }

write_status(){
  [ -n "$STATUS_FILE" ] || return 0
  # $3 (yellowPipelineUrl) is only populated when the backend returns it
  # (typically on terminal SUCCESS); blank otherwise.
  printf 'status=%s\nmsg=%s\nblueRecordId=%s\nyellowPipelineUrl=%s\n' \
    "$1" "$2" "$BRI" "${3:-}" > "$STATUS_FILE"
}

json_escape(){
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()),end="")'
}

PR_TITLE_J=$(printf '%s' "$PR_TITLE" | json_escape)
PR_DESC_J=$(printf '%s'  "$PR_DESC"  | json_escape)

# Resolve the full wheel URL via the manifest written by wheels.yml.
# pr-sync-collect runs after build-wheels in ci.yml, so the wheel and manifest
# are already uploaded to OBS by the time this script executes.
OBS_PREFIX="https://triton-ascend-artifacts.obs.cn-southwest-2.myhuaweicloud.com/triton-ascend-pr/pr_${PR_ID}"
WHEEL_NAME=$(curl -fsS "${OBS_PREFIX}/wheel-name.txt" | tr -d '[:space:]' || true)
if [ -z "${WHEEL_NAME}" ]; then
  log "wheel manifest missing at ${OBS_PREFIX}/wheel-name.txt"
  write_status "START_FAILURE" "wheel manifest missing under ${OBS_PREFIX}/ (build may have failed)"
  exit 1
fi
OBS_URL="${OBS_PREFIX}/${WHEEL_NAME}"
log "Resolved wheel URL: ${OBS_URL}"

log "=== Step 1: POST /start (blueRecordId=${BRI}) ==="
START_BODY=$(cat <<EOF
{
  "bluePipelineId": "null",
  "blueRecordId": "${BRI}",
  "blueRecordTaskId": "${BTI}",
  "blueRecordTaskName": "PR-${PR_ID}-CI",
  "yellowPipelineId": "${YELLOW_PIPELINE_ID}",
  "yellowGroupId": "${YELLOW_GROUP_ID}",
  "starter": "z00856207",
  "branch": "main",
  "parameter": {
    "pr": "${PR_ID}",
    "title": ${PR_TITLE_J},
    "description": ${PR_DESC_J},
    "url_b": "${URL_B}",
    "branch_b": "${BRANCH_Y}",
    "url_y": "${URL_Y}",
    "branch_y": "${BRANCH_Y}",
    "obs_url": "${OBS_URL}"
  }
}
EOF
)
# /start is non-idempotent: do not auto-retry (a 502 retry can collide with the
# already-accepted task and surface as "task already exists" on the backend).
R=$(curl -sS --max-time 30 \
  -o /tmp/by_start.body -w '%{http_code}' \
  -H "$H_CODE" -H "$H_KEY" -H "$H_SEC" -H "$H_JSON" \
  -X POST "${BASE_URL}/start" -d "${START_BODY}" || true)
BODY=$(cat /tmp/by_start.body 2>/dev/null || true)
log "HTTP=${R} response: ${BODY}"
if [ "$R" != "200" ] || ! echo "$BODY" | grep -q '"code":200'; then
  log "/start failed (HTTP=${R})"
  write_status "START_FAILURE" "HTTP=${R} body=${BODY}"
  exit 1
fi

log "=== Step 2: poll /query (interval ${POLL_WAIT}s, up to ${MAX_POLL} attempts) ==="
QUERY_BODY=$(cat <<EOF
{"blueRecordId":"${BRI}","blueRecordTaskId":"${BTI}"}
EOF
)

ST=""; MSG=""; DONE=false
for ((N=1; N<=MAX_POLL; N++)); do
  sleep "$POLL_WAIT"
  QHTTP=$(curl -sS --retry 3 --retry-all-errors --retry-delay 5 \
    -o /tmp/by_query.body -w '%{http_code}' \
    -H "$H_CODE" -H "$H_KEY" -H "$H_SEC" -H "$H_JSON" \
    -X POST "${BASE_URL}/query" -d "${QUERY_BODY}" || true)
  QR=$(cat /tmp/by_query.body 2>/dev/null || true)
  ST=$(echo "$QR"   | grep -o '"status":"[^"]*"'           | head -1 | cut -d'"' -f4)
  MSG=$(echo "$QR"  | grep -o '"msg":"[^"]*"'              | head -1 | cut -d'"' -f4)
  YPURL=$(echo "$QR" | grep -o '"yellowPipelineUrl":"[^"]*"' | head -1 | cut -d'"' -f4)
  log "[#${N}] HTTP=${QHTTP} status=${ST:-pending} msg=${MSG}"
  write_status "${ST:-PENDING}" "${MSG}" "${YPURL}"
  case "${ST}" in
    SUCCESS|FAILURE|MQS_SEND_FAILURE|UNAUTHORIZED|START_FAILURE|TIMEOUT|ABORT)
      DONE=true; break;;
  esac
done

echo "=============================="
if [ "$DONE" != "true" ]; then
  log "polling timed out, last status: ${ST:-unknown}"
  write_status "TIMEOUT" "polling exceeded ${MAX_POLL} attempts without a terminal status"
  exit 1
fi
log "terminal status: ${ST} - ${MSG}"
[ "$ST" = "SUCCESS" ] || exit 1
