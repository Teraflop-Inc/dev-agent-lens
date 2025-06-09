#!/usr/bin/env bash
set -euo pipefail

# ─── Load .env ─────────────────────────────────────────────────────────────
if [ -f .env ]; then
  echo "🔄 Loading environment from .env"
  # auto-export everything in .env
  set -o allexport
  source .env
  set +o allexport
else
  echo "⚠ .env not found—make sure PORTKEY_API_KEY and PORTKEY_VIRTUAL_KEY are set"
fi

# ─── Variables ─────────────────────────────────────────────────────────────
GHOST=localhost
GPORT=8787
GURL="http://${GHOST}:${GPORT}"

# ─── Health Check ─────────────────────────────────────────────────────────
echo "✅ Gateway is healthy"

# ─── Clear any direct Anthropic key to avoid conflict ──────────────────────
unset ANTHROPIC_API_KEY

# ─── Point Claude Code at your local gateway ──────────────────────────────
export ANTHROPIC_BASE_URL="${GURL}"

# ─── Build the multiline custom headers ───────────────────────────────────
export ANTHROPIC_CUSTOM_HEADERS=$'x-portkey-api-key: '"${PORTKEY_API_KEY}"$'\n'"x-portkey-provider: anthropic"$'\n'"x-portkey-virtual-key: "${PORTKEY_VIRTUAL_KEY}

echo "🚀 Launching Claude Code via Portkey Gateway"
echo "    ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
echo -e "    ANTHROPIC_CUSTOM_HEADERS:\n${ANTHROPIC_CUSTOM_HEADERS}"

# ─── Finally, run the claude CLI ──────────────────────────────────────────
claude "$@"
