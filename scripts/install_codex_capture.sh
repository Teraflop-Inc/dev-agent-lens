#!/usr/bin/env bash
# Install (or reinstall) the every-minute Codex capture job on this Mac (ENG2-402).
#   scripts/install_codex_capture.sh            install and run once now
#   scripts/install_codex_capture.sh --remove   uninstall
set -euo pipefail

LABEL=com.teraflop.dal-codex-capture
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$DEST"
  echo "[install-codex-capture] removed $LABEL"
  exit 0
fi

CONFIG="${DAL_CODEX_CONFIG:-$HOME/.dal/codex-capture.env}"
if [ ! -f "$CONFIG" ]; then
  echo "Create $CONFIG with DAL_CODEX_ENDPOINT and DAL_CODEX_INCLUDE first; see docs/codex-capture.md" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG"
: "${DAL_CODEX_ENDPOINT:?configure DAL_CODEX_ENDPOINT}"
: "${DAL_CODEX_INCLUDE:?configure DAL_CODEX_INCLUDE}"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
mkdir -p "$HOME/.dal" "$HOME/Library/LaunchAgents"
sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g" \
  "$REPO/scripts/launchd/$LABEL.plist" > "$DEST"
chmod +x "$REPO/scripts/codex_capture.sh"
launchctl bootstrap "$DOMAIN" "$DEST"
echo "[install-codex-capture] installed $DEST (every minute, starting now)"
echo "[install-codex-capture] log: $HOME/.dal/codex-capture.log"
