#!/usr/bin/env bash
# The ongoing accuracy check for the storage migration (ENG2-1609, 0003 §8 "oracle").
#
# Runs the cross-schema oracle over every configured phoenix-postgres source, cut at the
# top of the previous hour so the live producer and the import agree on a boundary, writes
# the verdict under ~/.dal/oracle/, and pages Slack on any mismatch. Same alert contract as
# agent-forge's capture-health workflow: plain {"text"}, short timeout, a broken alert never
# masks the check's exit code.
#
# Why a host job and not a GitHub schedule: the store is on this machine's MinIO until the
# net-new deployment (ENG2-1612) puts it somewhere a runner can reach. When it moves, this
# script moves into capture-health.yml unchanged; it already reads everything from env.
#
# Env comes from ~/.dal/oracle.env (chmod 600): PHOENIX_SQL_DATABASE_URL, AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY, optional SLACK_WEBHOOK_URL. Nothing here prints a credential.
set -uo pipefail
# launchd hands jobs a bare PATH; uv's installer puts it in ~/.local/bin (older: ~/.cargo/bin).
# Without this every run died on "uv: command not found" from 2026-09-12 to 09-21.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd "$(dirname "$0")/.."
OUT="${DAL_ORACLE_OUT:-$HOME/.dal/oracle}"
mkdir -p "$OUT"
if [ -f "$HOME/.dal/oracle.env" ]; then set -a; . "$HOME/.dal/oracle.env"; set +a; fi
UNTIL=$(date -u -v-1H '+%Y-%m-%dT%H:00:00Z' 2>/dev/null || date -u -d '1 hour ago' '+%Y-%m-%dT%H:00:00Z')
STAMP=$(date -u '+%Y-%m-%dT%H%MZ')
echo "[$(date -u '+%FT%TZ')] oracle start until=$UNTIL" >&2
uv run python scripts/migration_oracle.py --all-sources --until "$UNTIL" --json "$OUT/$STAMP.json" \
  > "$OUT/$STAMP.log" 2>&1
rc=$?
echo "[$(date -u '+%FT%TZ')] oracle exit=$rc log=$OUT/$STAMP.log" >&2
# Content-presence sentinel on the store itself (ENG2-1611): the AIT capture_health check,
# pointed at the new store. Exit 1 (breach) and 3 (could not run) fail the day, as they page
# in AIT's capture-health; 2 (no spans in the window) is an idle day and is only logged. A
# run with no verdict line never ran, and python's own exit 2 must not pass for "inconclusive".
HEALTH="$OUT/$STAMP.health"
uv run python scripts/store_health.py --hours "${DAL_HEALTH_HOURS:-24}" > "$HEALTH" 2>&1
hrc=$?
cat "$HEALTH" >> "$OUT/$STAMP.log"
if ! grep -qE '^(BREACH|healthy:|inconclusive:|operational:)' "$HEALTH"; then
  echo "operational: store_health gave no verdict (exit $hrc): $(tail -1 "$HEALTH")" >> "$OUT/$STAMP.log"
  hrc=3
fi
rm -f "$HEALTH"
echo "[$(date -u '+%FT%TZ')] store_health exit=$hrc" >&2
if [ "$hrc" -eq 1 ] || [ "$hrc" -eq 3 ]; then rc=1; fi
ln -sf "$STAMP.log" "$OUT/latest.log"; ln -sf "$STAMP.json" "$OUT/latest.json"
if [ "$rc" -ne 0 ]; then
  SUMMARY=$(grep -E 'MISMATCH|OVERALL|recipes agree|BREACH|healthy:|inconclusive|operational' "$OUT/$STAMP.log" | head -14)
  # A check that crashed prints none of those; its last lines are the reason.
  [ -n "$SUMMARY" ] || SUMMARY=$(tail -5 "$OUT/$STAMP.log")
  if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
    TEXT=$(printf '%s\n%s\n%s' \
      ":rotating_light: *DAL storage oracle FAILED* (exit $rc, until $UNTIL)" \
      "The store and Phoenix disagree on a cookbook question, the store content sentinel breached, or a check could not run. Log: $OUT/$STAMP.log" \
      "$SUMMARY")
    curl -sS -X POST "$SLACK_WEBHOOK_URL" --max-time 10 -H 'Content-Type: application/json' \
      --data "$(python3 -c 'import json,sys;print(json.dumps({"text":sys.stdin.read()}))' <<<"$TEXT")" \
      >/dev/null || echo "slack notify failed (check itself is unaffected)" >&2
  else
    echo "SLACK_WEBHOOK_URL unset; failure is only in $OUT" >&2
  fi
fi
exit "$rc"
