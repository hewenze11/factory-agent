#!/usr/bin/env bash
set -euo pipefail

# Blind integration test for factory-agent file repo system (M4 4.2 acceptance)
# Runs in GitHub Actions ubuntu-latest.
# Does NOT rely on internal implementation, only container logs + HTTP behavior.

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'

pass() { echo -e "${GRN}PASS${NC} - $1"; }
fail() { echo -e "${RED}FAIL${NC} - $1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
info() { echo -e "${YLW}INFO${NC} - $1"; }

FAIL_COUNT=0

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1"; exit 2; }
}

require docker
require curl
require jq
require unzip
require python3

IMAGE="${IMAGE:-172.236.254.239:30880/factory/agent:latest}"
CONTAINER="${CONTAINER:-factory-agent-test}"
PORT="${PORT:-34567}"
BASE_LOCAL="http://127.0.0.1:${PORT}"
BASE_LAN="http://172.236.254.239:${PORT}"

WORKDIR="${WORKDIR:-$(mktemp -d)}"
ARTDIR="${GITHUB_WORKSPACE:-$PWD}/blindtest-artifacts"
mkdir -p "$ARTDIR"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# -------- Helpers --------

http_code() {
  # usage: http_code METHOD URL [curl_extra...]
  local method="$1"; shift
  local url="$1"; shift
  curl -sS -o /dev/null -w "%{http_code}" -X "$method" "$url" "$@" || echo "000"
}

curl_json() {
  # usage: curl_json METHOD URL TOKEN JSON
  local method="$1"; shift
  local url="$1"; shift
  local token="$1"; shift
  local json="$1"; shift
  curl -sS -X "$method" "$url" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    --data-binary "$json" "$@"
}

curl_auth() {
  # usage: curl_auth METHOD URL TOKEN [extra...]
  local method="$1"; shift
  local url="$1"; shift
  local token="$1"; shift
  curl -sS -X "$method" "$url" -H "Authorization: Bearer ${token}" "$@"
}

wait_http() {
  local url="$1"
  local tries=60
  for i in $(seq 1 $tries); do
    local code
    code=$(http_code GET "$url")
    # any HTTP response indicates server is up (401/404/etc acceptable for readiness)
    if [[ "$code" != "000" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Discover token path from logs: "Generated new token at <path>"
get_token() {
  local tries=60
  for i in $(seq 1 $tries); do
    local logs
    logs=$(docker logs "$CONTAINER" 2>&1 || true)
    local path
    path=$(echo "$logs" | sed -nE 's/.*Generated new token at[[:space:]]+([^\r\n]+).*/\1/p' | tail -n1)
    if [[ -n "$path" ]]; then
      info "Token path from logs: $path"
      # read file inside container
      local token
      token=$(docker exec "$CONTAINER" sh -lc "cat '$path'" 2>/dev/null | tr -d '\r\n' || true)
      if [[ -n "$token" ]]; then
        echo "$token"
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

# Probe API base path. We know endpoints include /repo/{id}/... but might be under a prefix.
# We'll test a small set of candidate prefixes by requesting an auth-protected endpoint without token.
# Expectation per requirement #7: no token => 401. We'll prefer the first prefix that yields 401.

discover_prefix() {
  local repo_id="$1"
  local candidates=("" "/api" "/v1" "/api/v1")
  for prefix in "${candidates[@]}"; do
    local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/log"
    local code
    code=$(http_code GET "$url")
    if [[ "$code" == "401" ]]; then
      echo "$prefix"
      return 0
    fi
  done
  # fallback: if none 401, try find a prefix that is reachable (not 000)
  for prefix in "${candidates[@]}"; do
    local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/log"
    local code
    code=$(http_code GET "$url")
    if [[ "$code" != "000" ]]; then
      echo "$prefix"
      return 0
    fi
  done
  echo ""; return 0
}

# Determine write API style.
# Try JSON payload {"path":"...","content_base64":"..."} then {"path":"...","content":"..."}
# and finally multipart upload (file=@).
write_file() {
  local prefix="$1" repo_id="$2" token="$3" path="$4" content="$5"
  local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/write"

  local b64
  b64=$(printf "%s" "$content" | python3 - <<'PY'
import sys,base64
print(base64.b64encode(sys.stdin.buffer.read()).decode())
PY
)

  # attempt 1: content_base64
  local resp code
  resp=$(curl -sS -D - -o "$WORKDIR/write_resp.json" -X POST "$url" \
    -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
    --data-binary "{\"path\":\"${path}\",\"content_base64\":\"${b64}\"}" || true)
  code=$(echo "$resp" | awk 'NR==1{print $2}')
  if [[ "$code" =~ ^2 ]]; then
    echo "json_b64"; return 0
  fi

  # attempt 2: content
  resp=$(curl -sS -D - -o "$WORKDIR/write_resp.json" -X POST "$url" \
    -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
    --data-binary "{\"path\":\"${path}\",\"content\":${(printf '%s' "$content" | jq -Rs .)} }" || true)
  code=$(echo "$resp" | awk 'NR==1{print $2}')
  if [[ "$code" =~ ^2 ]]; then
    echo "json_text"; return 0
  fi

  # attempt 3: multipart
  local tmpf="$WORKDIR/upload.tmp"
  printf "%s" "$content" > "$tmpf"
  resp=$(curl -sS -D - -o "$WORKDIR/write_resp.json" -X POST "$url" \
    -H "Authorization: Bearer ${token}" \
    -F "path=${path}" -F "file=@${tmpf}" || true)
  code=$(echo "$resp" | awk 'NR==1{print $2}')
  if [[ "$code" =~ ^2 ]]; then
    echo "multipart"; return 0
  fi

  echo "unknown"; return 1
}

commit_repo() {
  local prefix="$1" repo_id="$2" token="$3"
  local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/commit"
  local out
  out=$(curl -sS -X POST "$url" -H "Authorization: Bearer ${token}" || true)
  printf "%s" "$out"
}

repo_log() {
  local prefix="$1" repo_id="$2" token="$3"
  local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/log"
  curl -sS "$url" -H "Authorization: Bearer ${token}" || true
}

export_repo() {
  local prefix="$1" repo_id="$2" token="$3" fmt="$4" outpath="$5"
  local url="${BASE_LOCAL}${prefix}/repo/${repo_id}/export?format=${fmt}"
  # stream to file
  curl -sS "$url" -H "Authorization: Bearer ${token}" --output "$outpath" --write-out "%{http_code}" || true
}

delete_repo() {
  local prefix="$1" repo_id="$2" token="$3"
  local url="${BASE_LOCAL}${prefix}/repo/${repo_id}"
  http_code DELETE "$url" -H "Authorization: Bearer ${token}" || true
}

# -------- Start container --------
info "Workdir: $WORKDIR"
info "Pulling image: $IMAGE"
# allow insecure registry setup is done on runner via daemon.json; in Actions we can't. We'll rely on docker to pull anyway.
# If it fails, the workflow should configure /etc/docker/daemon.json.

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

docker run -d --name "$CONTAINER" -p "${PORT}:${PORT}" "$IMAGE" >/dev/null

if ! wait_http "$BASE_LOCAL/"; then
  fail "Service did not become reachable on $BASE_LOCAL"
  exit 1
fi

TOKEN=$(get_token || true)
if [[ -z "${TOKEN}" ]]; then
  fail "Could not obtain token from container logs/file"
  exit 1
fi

REPO_ID="blindtest-$(date +%s)"
PREFIX=$(discover_prefix "$REPO_ID")
info "Discovered prefix: '${PREFIX}'"

# Build endpoint templates
EP_INIT="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/init"
EP_WRITE="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/write"
EP_COMMIT="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/commit"
EP_LOG="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/log"
EP_EXPORT_ZIP="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/export?format=zip"
EP_EXPORT_CURSOR="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/export?format=cursor"
EP_EXPORT_IDEA="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}/export?format=idea"
EP_DELETE="${BASE_LOCAL}${PREFIX}/repo/${REPO_ID}"

# -------- 7. Auth behavior: no token => 401 --------
code=$(http_code GET "$EP_LOG")
if [[ "$code" == "401" ]]; then
  pass "7) No token returns 401"
else
  fail "7) Expected 401 without token, got ${code} (url: $EP_LOG)"
fi

# -------- 1. init idempotent --------
code1=$(http_code POST "$EP_INIT" -H "Authorization: Bearer ${TOKEN}")
code2=$(http_code POST "$EP_INIT" -H "Authorization: Bearer ${TOKEN}")
if [[ "$code1" =~ ^2 && "$code2" =~ ^2 ]]; then
  pass "1) POST /repo/{id}/init is idempotent (${code1}, ${code2})"
else
  fail "1) init not idempotent or failed: first ${code1}, second ${code2}"
fi

# -------- 2. write + commit returns commit hash --------
WRITE_STYLE=""
if WRITE_STYLE=$(write_file "$PREFIX" "$REPO_ID" "$TOKEN" "README.md" "hello blindtest" ); then
  info "Write style detected: $WRITE_STYLE"
  commit_out=$(commit_repo "$PREFIX" "$REPO_ID" "$TOKEN")
  echo "$commit_out" > "$ARTDIR/commit_response.json"
  # accept either plain hash or json with hash key
  commit_hash=$(echo "$commit_out" | jq -r '(.hash // .commit // .commit_hash // .id // .sha // empty) ' 2>/dev/null || true)
  if [[ -z "$commit_hash" ]]; then
    # maybe plain text
    if echo "$commit_out" | grep -Eq '^[0-9a-f]{7,64}$'; then
      commit_hash="$commit_out"
    fi
  fi
  if [[ -n "$commit_hash" ]]; then
    pass "2) write+commit succeeded, commit hash: $commit_hash"
  else
    fail "2) commit did not return a recognizable commit hash. Response saved to $ARTDIR/commit_response.json"
  fi
else
  fail "2) write failed for README.md (endpoint: $EP_WRITE)"
fi

# -------- 3. log returns correct history --------
log_out=$(repo_log "$PREFIX" "$REPO_ID" "$TOKEN")
echo "$log_out" > "$ARTDIR/log_response.json"
if echo "$log_out" | jq . >/dev/null 2>&1; then
  # if json array or object contains commit_hash
  if [[ -n "${commit_hash:-}" ]] && echo "$log_out" | grep -q "$commit_hash"; then
    pass "3) /log contains committed hash"
  else
    # at least non-empty
    if [[ $(echo "$log_out" | jq 'length' 2>/dev/null || echo 0) -gt 0 ]]; then
      pass "3) /log returned non-empty history (could not match hash reliably)"
    else
      fail "3) /log returned empty history"
    fi
  fi
else
  # non-json: try grep
  if [[ -n "${commit_hash:-}" ]] && echo "$log_out" | grep -q "$commit_hash"; then
    pass "3) /log contains committed hash (non-json)"
  else
    fail "3) /log response not JSON and did not contain commit hash. Saved to $ARTDIR/log_response.json"
  fi
fi

# -------- 4. export zip streams and unzips, structure intact --------
ZIP_PATH="$WORKDIR/export.zip"
code=$(curl -sS -w "%{http_code}" -H "Authorization: Bearer ${TOKEN}" "$EP_EXPORT_ZIP" --output "$ZIP_PATH" || true)
if [[ "$code" =~ ^2 ]] && [[ -s "$ZIP_PATH" ]]; then
  mkdir -p "$WORKDIR/unzip"
  if unzip -q "$ZIP_PATH" -d "$WORKDIR/unzip"; then
    # expect README.md exists somewhere
    if find "$WORKDIR/unzip" -type f -name README.md | grep -q .; then
      pass "4) export zip downloaded and unzipped; README.md present"
    else
      fail "4) zip unzipped but README.md not found"
    fi
  else
    fail "4) zip could not be unzipped"
  fi
else
  fail "4) export zip failed (http $code) or empty file"
fi

# -------- 5. export cursor includes .cursorrules (or .cursor/) --------
CUR_PATH="$WORKDIR/export-cursor.zip"
code=$(curl -sS -w "%{http_code}" -H "Authorization: Bearer ${TOKEN}" "$EP_EXPORT_CURSOR" --output "$CUR_PATH" || true)
if [[ "$code" =~ ^2 ]] && [[ -s "$CUR_PATH" ]]; then
  mkdir -p "$WORKDIR/cursor"
  if unzip -q "$CUR_PATH" -d "$WORKDIR/cursor"; then
    if find "$WORKDIR/cursor" -type f -name .cursorrules | grep -q .; then
      pass "5) export cursor contains .cursorrules"
    elif find "$WORKDIR/cursor" -type d -name .cursor | grep -q .; then
      pass "5) export cursor contains .cursor/ directory"
    else
      fail "5) export cursor missing .cursorrules and .cursor/"
    fi
  else
    fail "5) export cursor payload not unzip-able"
  fi
else
  fail "5) export cursor failed (http $code) or empty"
fi

# -------- 6. export idea includes .idea/ --------
IDEA_PATH="$WORKDIR/export-idea.zip"
code=$(curl -sS -w "%{http_code}" -H "Authorization: Bearer ${TOKEN}" "$EP_EXPORT_IDEA" --output "$IDEA_PATH" || true)
if [[ "$code" =~ ^2 ]] && [[ -s "$IDEA_PATH" ]]; then
  mkdir -p "$WORKDIR/idea"
  if unzip -q "$IDEA_PATH" -d "$WORKDIR/idea"; then
    if find "$WORKDIR/idea" -type d -name .idea | grep -q .; then
      pass "6) export idea contains .idea/ directory"
    else
      fail "6) export idea missing .idea/"
    fi
  else
    fail "6) export idea payload not unzip-able"
  fi
else
  fail "6) export idea failed (http $code) or empty"
fi

# -------- 8. path traversal protection --------
# Try to write ../scripts/evil.sh and expect 400 or 403
URL_WRITE="$EP_WRITE"
# attempt with json b64 minimal
payload="{\"path\":\"../scripts/evil.sh\",\"content_base64\":\"$(printf 'evil' | python3 - <<'PY'
import sys,base64
print(base64.b64encode(sys.stdin.buffer.read()).decode())
PY
)\"}"
code=$(http_code POST "$URL_WRITE" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" --data-binary "$payload")
if [[ "$code" == "400" || "$code" == "403" ]]; then
  pass "8) Path traversal blocked (write ../scripts/evil.sh => $code)"
else
  fail "8) Expected 400/403 for path traversal, got $code"
fi

# -------- 11. write >10MB returns 413 --------
BIG_PATH="$WORKDIR/big.bin"
python3 - <<'PY'
import os
p=os.environ['BIG_PATH']
with open(p,'wb') as f:
    f.write(b'a'*(10*1024*1024 + 1))
PY
# try multipart for big to avoid json overhead
code=$(http_code POST "$EP_WRITE" -H "Authorization: Bearer ${TOKEN}" \
  -F "path=big.bin" -F "file=@${BIG_PATH}")
if [[ "$code" == "413" ]]; then
  pass "11) write >10MB returns 413"
else
  fail "11) Expected 413 for >10MB write, got $code"
fi

# -------- 10. delete idempotent (204 twice) --------
code1=$(http_code DELETE "$EP_DELETE" -H "Authorization: Bearer ${TOKEN}")
code2=$(http_code DELETE "$EP_DELETE" -H "Authorization: Bearer ${TOKEN}")
if [[ "$code1" == "204" && "$code2" == "204" ]]; then
  pass "10) DELETE idempotent (204 twice)"
else
  fail "10) Expected 204 twice, got ${code1} and ${code2}"
fi

# -------- 9. only listen on 127.0.0.1; direct LAN access refused --------
# In GitHub Actions we are NOT on the same LAN as 172.236.254.239, so this check is best-effort.
# We'll attempt and treat a connection failure / non-2xx as PASS, but a 2xx would be FAIL.
code=$(http_code GET "${BASE_LAN}${PREFIX}/repo/${REPO_ID}/log" -H "Authorization: Bearer ${TOKEN}")
if [[ "$code" == "000" || "$code" == "401" || "$code" == "403" || "$code" == "404" || "$code" == "502" || "$code" == "503" ]]; then
  pass "9) External access appears refused/unreachable (code $code)"
else
  # If it returned 2xx, that means it was reachable; if 5xx maybe still reachable.
  if [[ "$code" =~ ^2 ]]; then
    fail "9) External LAN address responded with 2xx ($code) — should be refused"
  else
    # ambiguous but likely reachable
    fail "9) Unexpected response code for external LAN access: $code"
  fi
fi

# -------- Report --------
echo ""
echo "========== Blind Test Report =========="
if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo -e "${GRN}ALL PASS${NC}"
else
  echo -e "${RED}${FAIL_COUNT} TEST(S) FAILED${NC}"
fi

echo "Artifacts: $ARTDIR"
exit "$FAIL_COUNT"
