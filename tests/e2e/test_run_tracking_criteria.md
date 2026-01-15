# End-to-End Test Criteria for Unified Run Tracking (ENG2-662)

## Test Source
- **Primary:** `phoenix-local-alex` (Phoenix @ http://localhost:6006)
- **Alternative:** `phoenix-default-alex` (same server, different project)

## Phase 1: Staleness Detection

### Test 1.1: Stale `current_batch` Detection
**Scenario:** Process killed during sync leaves stale `current_batch`

**Setup:**
1. Start sync-historical with `--days 7`
2. Wait for `current_batch` to be set
3. Kill the process (Ctrl+C or `kill -9`)
4. Check state file has `current_batch` set

**Test:**
```bash
dal sync-historical --status
```

**Expected Before Fix:** Shows "in progress" (incorrect)
**Expected After Fix:** Shows "paused" or "stale" with warning about stale batch

### Test 1.2: Staleness Timeout Threshold
**Scenario:** Batch older than threshold is considered stale

**Setup:**
1. Manually edit state file to set `updated_at` to 10 minutes ago
2. Set `current_batch` to a valid range

**Test:**
```bash
dal sync-historical --status
```

**Expected:** Status shows "paused" (stale detected), not "in progress"

### Test 1.3: Active Process Still Shows In Progress
**Scenario:** Currently running sync shows correct status

**Setup:**
1. Start sync-historical in background
2. Note the PID

**Test:**
```bash
dal sync-historical --status
```

**Expected:** Shows "in progress" with PID information (when PID tracking added)

## Phase 2: Run Tracking Infrastructure

### Test 2.1: Run ID Generation
**Scenario:** Each sync run gets a unique identifier

**Setup:**
1. Run `dal sync-historical --source phoenix-local-alex --days 1`
2. Note the run ID displayed

**Test:**
1. Check state file contains `run_id`
2. Run again, verify new `run_id`

**Expected:**
- Run ID format: `{source}-{YYYYMMDD}-{HHMMSS}-{random}` (e.g., `phoenix-local-alex-20260106-143022-a7b3`)
- Each run gets unique ID

### Test 2.2: PID Tracking
**Scenario:** Running process PID is stored

**Setup:**
1. Start sync-historical in background

**Test:**
```bash
cat ~/.dal/state/historical-sync-phoenix-local-alex.json | jq '.current_run.pid'
ps aux | grep <pid>
```

**Expected:** PID in state file matches actual running process

### Test 2.3: PID Liveness Check
**Scenario:** Dead PID is detected

**Setup:**
1. Manually set PID in state file to non-existent process

**Test:**
```bash
dal sync-historical --status
```

**Expected:** Detects PID is dead, shows appropriate status

## Phase 3: Unified Status

### Test 3.1: Status Shows Both Sync Types
**Scenario:** `dal sync-historical --status` shows history for both `sync` and `sync-historical`

**Setup:**
1. Run `dal sync --source phoenix-local-alex --days 1`
2. Run `dal sync-historical --source phoenix-local-alex --days 1`

**Test:**
```bash
dal sync-historical --status
```

**Expected:** Shows status/history for both sync operations

### Test 3.2: Run History Display
**Scenario:** Previous runs are visible

**Setup:**
1. Complete 3 sync-historical runs
2. Complete 2 sync runs

**Test:**
```bash
dal sync-historical --status --history
```

**Expected:** Shows last N runs with run_id, start/end time, status, span count

## Phase 4: Purge Command

### Test 4.1: Dry Run Lists Files
**Scenario:** `dal purge --dry-run` shows files without deleting

**Setup:**
1. Ensure source has data in:
   - `~/.dal/data/raw/phoenix-local-alex/`
   - `~/.dal/data/parquet/phoenix-local-alex_*.parquet`
   - `~/.dal/state/historical-sync-phoenix-local-alex.json`

**Test:**
```bash
dal purge --source phoenix-local-alex --dry-run
```

**Expected Output:**
```
Files that would be deleted for source 'phoenix-local-alex':

State files:
  ~/.dal/state/historical-sync-phoenix-local-alex.json (2KB)

Raw data:
  ~/.dal/data/raw/phoenix-local-alex/spans.jsonl (150MB)
  ~/.dal/data/raw/phoenix-local-alex/sessions.jsonl (10MB)

Parquet files:
  ~/.dal/data/parquet/phoenix-local-alex_spans.parquet (45MB)
  ~/.dal/data/parquet/phoenix-local-alex_sessions.parquet (3MB)

Total: 5 files, ~208MB

To delete these files, run: dal purge --source phoenix-local-alex
```

### Test 4.2: Purge Without --dry-run Shows Warning
**Scenario:** Running without --dry-run prompts for confirmation

**Test:**
```bash
dal purge --source phoenix-local-alex
```

**Expected:** Shows same file list, asks for confirmation (Y/N)

### Test 4.3: Purge --all Lists All Sources
**Scenario:** Shows all sources' files

**Test:**
```bash
dal purge --all --dry-run
```

**Expected:** Lists files for all configured sources

## Integration Tests

### Test I1: Full Workflow - Clean Start to Completion
**Steps:**
1. `dal purge --source phoenix-local-alex --dry-run` (verify clean state)
2. Delete state file manually
3. `dal sync-historical --source phoenix-local-alex --days 1`
4. Verify run_id is created
5. `dal sync-historical --status`
6. Verify shows "in progress" or "complete"
7. Kill process mid-sync
8. `dal sync-historical --status`
9. Verify shows "paused" (not "in progress")
10. Resume: `dal sync-historical --source phoenix-local-alex`
11. Verify resumes with same/new run_id
12. Complete sync
13. `dal sync-historical --status`
14. Verify shows "complete"

### Test I2: Process Kill and Resume
**Steps:**
1. Start long sync (`--days 30`)
2. Wait for 3 batches to complete
3. `kill -9 <pid>`
4. Verify state file has `current_batch` set
5. `dal sync-historical --status`
6. Verify shows stale warning
7. Resume sync
8. Verify picks up from checkpoint, not from stale batch

### Test I3: Out-of-Band Status Check
**Steps:**
1. Start sync in one terminal
2. In another terminal, run `dal sync-historical --status`
3. Verify shows real-time progress
4. Verify shows run_id for reference

## Acceptance Criteria Summary

| ID | Criteria | Test |
|----|----------|------|
| AC1 | Stale `current_batch` doesn't show as "in progress" | 1.1, 1.2 |
| AC2 | Active process shows correct "in progress" status | 1.3 |
| AC3 | Each run has unique identifier | 2.1 |
| AC4 | PID is tracked and validated | 2.2, 2.3 |
| AC5 | Status command shows real-time progress | 3.1 |
| AC6 | Run history is available | 3.2 |
| AC7 | Purge --dry-run lists files without deletion | 4.1 |
| AC8 | Purge shows confirmation prompt | 4.2 |
| AC9 | Full workflow works end-to-end | I1 |
| AC10 | Kill and resume works correctly | I2 |
