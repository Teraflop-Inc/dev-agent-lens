#!/usr/bin/env bash
# Push this laptop's recent Codex sessions to the DAL receiver (ENG2-402, path A).
#
# Runs every minute from launchd (scripts/launchd/com.teraflop.dal-codex-capture.plist). Codex
# writes every session to ~/.codex/sessions whether it ran in the CLI, the desktop app or
# the IDE extension, so this one job covers all three.
#
# Safe to re-run: span ids come from the session and step number, and the store dedupes on
# (span_id, source), so a session you kept working on only adds its new steps.
#
# Settings (environment, all optional):
#   DAL_CODEX_ENDPOINT  receiver base URL        (required: your receiver)
#   DAL_CODEX_INCLUDE   cwd glob to capture      (required: the repositories you choose)
#   DAL_CODEX_PROJECT   source stamp in the store (default: codex-sessions)
#   DAL_CODEX_DAYS      lookback in days         (default: 2, so time off the tailnet catches up)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# launchd does not inherit an interactive shell's variables. Keep the selected
# destination and capture scope in a private local config file.
CONFIG="${DAL_CODEX_CONFIG:-$HOME/.dal/codex-capture.env}"
if [ -f "$CONFIG" ]; then
  # shellcheck source=/dev/null
  source "$CONFIG"
fi
ENDPOINT="${DAL_CODEX_ENDPOINT:?set DAL_CODEX_ENDPOINT in ~/.dal/codex-capture.env}"
INCLUDE="${DAL_CODEX_INCLUDE:?set DAL_CODEX_INCLUDE to the repositories you want captured}"
PROJECT="${DAL_CODEX_PROJECT:-codex-sessions}"
DAYS="${DAL_CODEX_DAYS:-2}"
SINCE="$(date -v-"${DAYS}"d +%Y-%m-%d 2>/dev/null || date -d "-${DAYS} days" +%Y-%m-%d)"

log() { echo "[$(date '+%F %T')] [codex-capture] $*"; }

# If the receiver is temporarily unreachable, the lookback catches up on the next run.
if ! curl -s -m 5 -o /dev/null "$ENDPOINT/v1/traces"; then
  log "receiver $ENDPOINT unreachable; skipping this run"
  exit 0
fi

started=$(date +%s)
log "start include=$INCLUDE since=$SINCE endpoint=$ENDPOINT project=$PROJECT"
cd "$REPO"
uv run --quiet --extra otlp dal ingest-sessions --agent codex \
  --include "$INCLUDE" --since "$SINCE" \
  --endpoint "$ENDPOINT" --project "$PROJECT" --yes 2>&1 \
  | grep -vE "DEBUG" | tail -5
log "done in $(( $(date +%s) - started ))s"
