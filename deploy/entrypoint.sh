#!/usr/bin/env bash
# One image, three jobs. Which one is the first argument.
#   sync-loop    pull every configured source into the store every SYNC_INTERVAL seconds
#   oracle       run the accuracy check once (compose schedules it)
#   dal ...      anything else is passed to the dal CLI
set -uo pipefail
mkdir -p /data/dal /data/oracle
configure() {
  # Sources are declared once from env: DAL_SOURCES="name=phoenix-project,name2=project2".
  # Re-running is a no-op per source; `dal config add-source` refuses to duplicate.
  [ -n "${DAL_SOURCES:-}" ] || return 0
  IFS=',' read -ra pairs <<< "$DAL_SOURCES"
  for pair in "${pairs[@]}"; do
    name="${pair%%=*}"; project="${pair#*=}"
    dal config list-sources 2>/dev/null | grep -q "^${name} " && continue
    dal config add-source "$name" --type phoenix-postgres --project "$project" --shared >/dev/null 2>&1 \
      && echo "[entrypoint] source $name -> $project" || echo "[entrypoint] source $name already present or failed to add" >&2
  done
}
VERDICT='^(BREACH|healthy:|inconclusive:|operational:)'
sentinel() {
  # The content-presence sentinel (same thresholds and exit codes as AIT capture_health):
  # 0 healthy, 1 breach, 2 inconclusive (no spans in the window), 3 operational. Its output is
  # appended to the day's log ($1) and its verdict line goes to stdout, where
  # `docker compose logs dal-oracle` shows it. A run that prints no verdict never ran (a
  # missing script, an import error); python's own exit 2 must not pass for "inconclusive".
  # That is how this check failed silently every day from 2026-09-11 to 09-21.
  local out="$1.health" hrc
  python scripts/store_health.py --hours "${DAL_HEALTH_HOURS:-24}" > "$out" 2>&1
  hrc=$?
  cat "$out" >> "$1"
  if ! grep -E "$VERDICT" "$out"; then
    echo "operational: store_health gave no verdict (exit $hrc): $(tail -1 "$out")" | tee -a "$1"
    hrc=3
  fi
  rm -f "$out"
  return "$hrc"
}
notify() {
  # Plain {"text"} with a short timeout, as AIT capture-health posts it. A broken alert never
  # masks the check's exit code, and an unset webhook is said out loud rather than skipped.
  if [ -z "${SLACK_WEBHOOK_URL:-}" ]; then
    echo "[entrypoint] SLACK_WEBHOOK_URL unset; nobody was paged"
    return 0
  fi
  local body
  body=$(printf '%s' "$1" | python -c 'import json,sys;print(json.dumps({"text":sys.stdin.read()}))')
  curl -sS -X POST "$SLACK_WEBHOOK_URL" --max-time 10 -H 'Content-Type: application/json' --data "$body" >/dev/null \
    || echo "[entrypoint] slack notify failed (the check's exit code stands)" >&2
}
case "${1:-sync-loop}" in
  sync-loop)
    configure
    dal store layout "${DAL_SPAN_LAYOUT:-raw}" >/dev/null 2>&1 || true
    # The store's lower bound is the first pull's start date, day-aligned in UTC so the
    # daily check can cut the producer at the same instant. Written once; later pulls
    # re-fetch a SYNC_OVERLAP_DAYS window and the store lands each span once.
    SINCE_FILE=/data/dal/sync_since
    if [ ! -s "$SINCE_FILE" ]; then
      date -u -d "${SYNC_DAYS:-2} days ago" '+%Y-%m-%d' > "$SINCE_FILE"
      echo "[entrypoint] first pull starts at $(cat "$SINCE_FILE") 00:00Z (SYNC_DAYS=${SYNC_DAYS:-2})"
    fi
    first=1
    while true; do
      if [ -z "${DAL_SOURCES:-}" ]; then
        # Receiver-only or folder-only deployment: nothing to pull, just keep typed fresh.
        echo "[$(date -u '+%FT%TZ')] no DAL_SOURCES; typed rebuild only"
      elif [ "$first" = 1 ]; then
        echo "[$(date -u '+%FT%TZ')] sync all sources from $(cat "$SINCE_FILE")"
        dal sync --all-sources --start-date "$(cat "$SINCE_FILE")" --delay 0.5 2>&1 | grep -E 'Total spans|Span store|FAILED|Sync complete' || true
        first=0
      else
        echo "[$(date -u '+%FT%TZ')] sync all sources (last ${SYNC_OVERLAP_DAYS:-1} day(s), idempotent)"
        dal sync --all-sources --days "${SYNC_OVERLAP_DAYS:-1}" --delay 0.5 2>&1 | grep -E 'Total spans|Span store|FAILED|Sync complete' || true
      fi
      if [ -n "${LINEAR_API_KEY:-}" ]; then
        dal linear-sync 2>&1 | tail -1 || true
      fi
      if [ "${TYPED_REBUILD:-1}" = "1" ]; then
        DAL_SPAN_LAYOUT=typed dal store verify --from-parquet "${DAL_RAW_GLOB:?set DAL_RAW_GLOB to the spans_raw glob of the store}" 2>&1 | tail -1
      fi
      sleep "${SYNC_INTERVAL:-900}"
    done ;;
  oracle)
    configure
    STAMP=$(date -u '+%Y-%m-%dT%H%MZ'); LOG="/data/oracle/$STAMP.log"
    if [ -z "${PHOENIX_SQL_DATABASE_URL:-}" ]; then
      # No producer to compare against (a receiver-only or folder-only deployment):
      # the accuracy check has nothing to diff, so only the content sentinel runs.
      echo "[entrypoint] no PHOENIX_SQL_DATABASE_URL; skipping the oracle, running store_health"
      sentinel "$LOG"; hrc=$?
      # 1 (breach) and 3 (could not run) page, as in AIT capture-health; 2 is an idle window.
      if [ "$hrc" -eq 1 ] || [ "$hrc" -eq 3 ]; then
        notify ":rotating_light: DAL store content sentinel failed (exit $hrc; 1 = breach, 3 = could not run). See $LOG"
      fi
      exit "$hrc"
    fi
    UNTIL=$(date -u -d '1 hour ago' '+%Y-%m-%dT%H:00:00Z')
    # The store holds what the sync loop pulled since its first start date, not history;
    # compare that window on both sides or the producer is ahead for a true reason.
    # ORACLE_DAYS overrides the lower bound.
    if [ -n "${ORACLE_DAYS:-}" ]; then
      SINCE=$(date -u -d "$ORACLE_DAYS days ago" '+%Y-%m-%dT00:00:00Z')
    elif [ -s /data/dal/sync_since ]; then
      SINCE="$(cat /data/dal/sync_since)T00:00:00Z"
    else
      SINCE=$(date -u -d "${SYNC_DAYS:-2} days ago" '+%Y-%m-%dT00:00:00Z')
    fi
    python scripts/migration_oracle.py --all-sources --since "$SINCE" --until "$UNTIL" --layout "${DAL_SPAN_LAYOUT:-typed}" --json "/data/oracle/$STAMP.json" 2>&1 | tee "$LOG" | grep -E '^==|agree|OVERALL|MISMATCH'
    orc=${PIPESTATUS[0]}
    if [ "$orc" -ne 0 ] && ! grep -qE 'recipes agree|OVERALL' "$LOG"; then
      echo "[entrypoint] oracle gave no verdict (exit $orc): $(tail -1 "$LOG")"
    fi
    sentinel "$LOG"; hrc=$?
    rc=$orc
    if [ "$hrc" -eq 1 ] || [ "$hrc" -eq 3 ]; then rc=1; fi
    if [ "$rc" -ne 0 ]; then
      notify ":rotating_light: DAL storage check FAILED (oracle exit $orc, sentinel exit $hrc, until $UNTIL). See $LOG"
    fi
    exit "$rc" ;;
  *)
    # `command: ["dal", "otlp-receive", ...]` in compose reaches here with "dal" as $1;
    # a bare subcommand does too. Accept both.
    [ "${1:-}" = "dal" ] && shift
    exec dal "$@" ;;
esac
