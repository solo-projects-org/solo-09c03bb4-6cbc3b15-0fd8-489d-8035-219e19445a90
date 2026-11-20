#!/usr/bin/env bash
# Cross-station review (复核) demo against a running archive stack.
#
#   ./scripts/demo_review.sh          # uses http://localhost:8080
#   ARCHIVE_URL=...
#
# Walks through: intake rules, two-station consensus issuing a review number,
# serving data by review number, immediate invalidation when support is lost,
# and re-issuing a new number when mutual support recovers.
#
# Minute verdicts are immutable snapshots: an intake only finalises minutes
# that have elapsed, and the "open" minute is settled with an explicit
# evaluation call, so this demo drives minute 0..3 one step at a time.
set -euo pipefail

ARCHIVE_URL="${ARCHIVE_URL:-http://localhost:8080}"
DAY="${DAY:-2026-11-19}"
BASE="$ARCHIVE_URL/api/review/days/$DAY"

banner() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

submit() {
  # submit <station> <submission_no> <notices-json> [complete]
  local station="$1" no="$2" notices="$3" complete="${4:-true}"
  curl -fsS -X POST "$BASE/submissions" \
    -H 'content-type: application/json' \
    -d "{\"station\": \"$station\", \"submission_no\": \"$no\", \"complete\": $complete, \"notices\": $notices}"
}

evaluate() { curl -fsS -X POST "$BASE/evaluation" -H 'content-type: application/json' -d "{\"minute\": $1}"; }

LIST_A='[
  {"announcement_no":"HB-EIA-2026-1119-01","title":"云岭风电场二期工程环境影响报告书公示","published_at":"2026-11-19T08:00:00Z"},
  {"announcement_no":"HB-EIA-2026-1119-02","title":"青石镇污水处理厂提标改造竣工验收公示","published_at":"2026-11-19T12:00:00Z"}
]'
LIST_A_REVERSED='[
  {"announcement_no":"HB-EIA-2026-1119-02","title":"青石镇污水处理厂提标改造竣工验收公示","published_at":"2026-11-19T12:00:00Z"},
  {"announcement_no":"HB-EIA-2026-1119-01","title":"云岭风电场二期工程环境影响报告书公示","published_at":"2026-11-19T08:00:00Z"}
]'
LIST_B='[
  {"announcement_no":"HB-EIA-2026-1119-01","title":"云岭风电场二期工程环境影响报告书公示（修订版）","published_at":"2026-11-19T08:00:00Z"},
  {"announcement_no":"HB-EIA-2026-1119-02","title":"青石镇污水处理厂提标改造竣工验收公示","published_at":"2026-11-19T12:00:00Z"}
]'

banner "1. Station A delivers; minute 0 closes with only one station (evidence)"
submit station-a S-001 "$LIST_A" | jq '{station, submission_no, accepted, notice_count}'
evaluate 0 | jq '{minute: .minute_label, status, review_no, detail}'

banner "2. Station B delivers the same list in a different row order; minute 1 goes active"
submit station-b T-001 "$LIST_A_REVERSED" | jq '{station, submission_no, accepted}'
minute1=$(evaluate 1)
echo "$minute1" | jq '{minute: .minute_label, status, review_no, supporting_stations}'
REVIEW_NO=$(echo "$minute1" | jq -r .review_no)

banner "3. Fetch the verified data by review number: $REVIEW_NO"
curl -fsS "$ARCHIVE_URL/api/review/numbers/$REVIEW_NO" | jq .

banner "4. Identical resend is not re-accepted (accepted=false)"
submit station-a S-001 "$LIST_A" | jq '{station, submission_no, accepted}'

banner "5. Old number and same-number-changed-payload are both rejected (HTTP 409)"
curl -s -o /dev/null -w 'stale number -> %{http_code}\n' \
  -X POST "$BASE/submissions" -H 'content-type: application/json' \
  -d "{\"station\":\"station-a\",\"submission_no\":\"S-000\",\"complete\":true,\"notices\":$LIST_B}"
curl -s -o /dev/null -w 'same number, changed payload -> %{http_code}\n' \
  -X POST "$BASE/submissions" -H 'content-type: application/json' \
  -d "{\"station\":\"station-a\",\"submission_no\":\"S-001\",\"complete\":true,\"notices\":$LIST_B}"

banner "6. Station B changes its list at minute 2: mutual support is lost"
submit station-b T-002 "$LIST_B" | jq '{station, submission_no, accepted}'
evaluate 2 | jq '{minute: .minute_label, status, detail}'
echo "The old review number can no longer serve data:"
curl -s -w '\nHTTP %{http_code}\n' "$ARCHIVE_URL/api/review/numbers/$REVIEW_NO"

banner "7. Station B restores the original list: a NEW review number is issued at minute 3"
submit station-b T-003 "$LIST_A" | jq '{station, submission_no, accepted}'
minute3=$(evaluate 3)
echo "$minute3" | jq '{minute: .minute_label, status, review_no, supporting_stations}'
NEW_NO=$(echo "$minute3" | jq -r .review_no)
test "$NEW_NO" != "$REVIEW_NO" && echo "new number $NEW_NO differs from old $REVIEW_NO"
curl -fsS "$ARCHIVE_URL/api/review/numbers/$NEW_NO" | jq '{review_no, day, notices: [.notices[].announcement_no]}'

banner "8. Full per-minute timeline for the day"
curl -fsS "$BASE/minutes" | jq -r '.[] | "\(.minute_label)  \(.status)  \(.review_no // "-")  [\(.supporting_stations | join(","))]"'

banner "Swagger UI"
echo "archive (review endpoints under /api/review): $ARCHIVE_URL/docs"
