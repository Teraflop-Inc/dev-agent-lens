#!/bin/bash
# Demonstration script for running the full e2e validation flow
# This shows how to validate an existing session from phoenix-local

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================================${NC}"
echo -e "${BLUE}Claude-Lens Proxy Pipeline - E2E Validation Demo${NC}"
echo -e "${BLUE}=====================================================================${NC}"
echo

# Check if session ID provided
if [ -z "$1" ]; then
    echo -e "${YELLOW}No session ID provided. Will find a recent session...${NC}"
    echo

    # Get a recent session ID from phoenix-local
    SESSION_ID=$(uv run python -c "
import pyarrow.parquet as pq
import sys
from pathlib import Path

try:
    table = pq.read_table(str(Path.home() / '.dal' / 'data' / 'parquet' / 'phoenix-local-alex_sessions.parquet'))
    df = table.to_pandas()

    # Get session with reasonable span count (not too small, not too large)
    df_filtered = df[(df['span_count'] > 10) & (df['span_count'] < 500)]

    if len(df_filtered) > 0:
        session_id = df_filtered.iloc[0]['session_id']
        span_count = df_filtered.iloc[0]['span_count']
        print(session_id, file=sys.stderr)
        print(f'Found session: {session_id} ({span_count} spans)', file=sys.stderr)
        print(session_id)
    else:
        print('No suitable sessions found', file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1 | tail -1)

    if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "No suitable sessions found" ]; then
        echo -e "${RED}✗ Could not find a suitable test session${NC}"
        echo
        echo "Please provide a session ID as an argument:"
        echo "  $0 <session-id>"
        echo
        echo "Or create a test session by running:"
        echo "  ~/Company/dev3/private-dev-agent-lens/claude-lens"
        echo "  # Then type a simple prompt like: 'What is 2+2?'"
        exit 1
    fi
else
    SESSION_ID="$1"
fi

echo -e "${GREEN}✓ Using session ID: ${SESSION_ID}${NC}"
echo

# Step 1: Infrastructure Check
echo -e "${BLUE}Step 1: Checking infrastructure...${NC}"
uv run python tests/e2e/test_proxy_pipeline.py | grep "✓"
echo

# Step 2: Run validation
echo -e "${BLUE}Step 2: Running validation test...${NC}"
echo
TEST_SESSION_ID="$SESSION_ID" uv run pytest \
    tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export \
    -v -s

echo
echo -e "${BLUE}=====================================================================${NC}"
echo -e "${GREEN}✓ E2E validation completed successfully!${NC}"
echo -e "${BLUE}=====================================================================${NC}"
echo
echo "Next steps:"
echo "  1. Review the validation report above"
echo "  2. Check exported markdown: ~/.dal/exports/"
echo "  3. Analyze session: uv run dal analyze-tokens --session ${SESSION_ID}"
echo "  4. Export chain: uv run dal chain-export --session ${SESSION_ID}"
echo
