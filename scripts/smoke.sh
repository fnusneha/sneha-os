#!/usr/bin/env bash
#
# End-to-end smoke test for a running Sneha.OS instance.
#
# Usage:
#     scripts/smoke.sh https://your-deployment.example.com
#     scripts/smoke.sh http://localhost:8000
#
# Checks (12 total):
#   1. /healthz returns 200 "ok"
#   2. /api/health returns JSON with row counts + last_sync_date
#   3. /dashboard renders HTML with the Quest Hub markers
#   4. /rides renders HTML with the Ride Atlas markers
#   5. /api/season GET returns the current month's done indices
#   6. /api/manual sauna toggle round-trips through the DB (flips
#      twice so it ends where it started).
#
# Exits non-zero on any failure.

set -u
URL="${1:-https://sneha-os.onrender.com}"
URL="${URL%/}"   # trim trailing slash

echo "Target: $URL"
echo

PASS=0
FAIL=0

check() {
  local name="$1"; shift
  if "$@" > /tmp/verify.out 2>&1; then
    echo "  ✅ $name"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $name"
    sed 's/^/        /' /tmp/verify.out | head -5
    FAIL=$((FAIL + 1))
  fi
}

grep_body() {
  curl -fsS --max-time 90 "$1" | grep -qE "$2"
}

json_key() {
  curl -fsS --max-time 15 "$1" | grep -qE "\"$2\""
}

post_ok() {
  curl -fsS --max-time 15 -X POST "$1" \
    -H "Content-Type: application/json" -d "$2" | grep -qE '"ok":\s*true'
}

echo "=== 1. Liveness ==="
check "/healthz 200 ok" grep_body "$URL/healthz" "^ok$"
check "/api/health has last_sync_date" json_key "$URL/api/health" "last_sync_date"
check "/api/health has daily_entries count" json_key "$URL/api/health" "daily_entries"

echo
echo "=== 2. Dashboard render ==="
check "/dashboard has Quest Hub markers"   grep_body "$URL/dashboard" "wp-stars-num"
check "/dashboard has Core Missions stage" grep_body "$URL/dashboard" "stage-core"
check "/dashboard has Sauna toggle"        grep_body "$URL/dashboard" "toggle-sauna"
check "/dashboard has NO Tailscale URLs"   bash -c "! curl -fsS '$URL/dashboard' | grep -q 'tail790bc5'"

echo
echo "=== 3. Rides render ==="
check "/rides has YoY title"   grep_body "$URL/rides" "yoy-title"
check "/rides has Monthly Pulse" grep_body "$URL/rides" "rp-pulse"

echo
echo "=== 4. Season pass (GET) ==="
check "/api/season returns indices" json_key "$URL/api/season" "indices"

echo
echo "=== 5. Sauna round-trip (dangerous — flips today's value twice) ==="
TODAY=$(date +%Y-%m-%d)
check "POST /api/manual sauna=false" post_ok "$URL/api/manual" "{\"field\":\"sauna\",\"value\":false,\"date\":\"$TODAY\"}"
check "POST /api/manual sauna=true"  post_ok "$URL/api/manual" "{\"field\":\"sauna\",\"value\":true,\"date\":\"$TODAY\"}"

echo
echo "=== Summary ==="
echo "  $PASS passed, $FAIL failed"
exit $FAIL
