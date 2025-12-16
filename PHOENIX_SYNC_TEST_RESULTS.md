# Phoenix Historical Sync Test Results
**Date:** 2025-12-16
**Project:** dev-agent-lens
**Testing:** Phoenix Local historical sync functionality

## Executive Summary

✅ **Phoenix local is running and operational**
⚠️ **Historical sync is partially working with timeout issues**
✅ **Successfully synced 13,659+ spans from Phoenix local**
⚠️ **5 out of 15 batches failed due to timeouts/connection resets**

## Test Environment

### Phoenix Container Status
- **Container:** `dev-agent-lens-phoenix-1`
- **Status:** Running (Up 7 minutes at test time)
- **Image:** `arizephoenix/phoenix:latest`
- **Ports:** 6006 (UI), 4317 (OTLP gRPC)
- **Health:** Unhealthy (but functional)
- **Storage:** `sqlite:////root/.phoenix/phoenix.db`

### Environment Configuration
```bash
DAL_PHOENIX_URL=http://localhost:6006
DAL_PHOENIX_PROJECT=dev-agent-lens
```

## Test Results

### 1. Phoenix Client Direct Testing

**Test 1: No time filter (limit=100)**
- ✅ Success: 100 spans fetched
- Time range in data: 2025-12-16 19:37:55 to 2025-12-16 19:38:28

**Test 2: With time filter (last 1 hour)**
- ✅ Success: 100 spans fetched

**Test 3: Very recent (last 10 minutes)**
- ✅ Success: 100 spans fetched

**Conclusion:** Phoenix client works correctly with and without time filters when fetching small datasets.

### 2. Phoenix Projects Available

**Project: default**
- Total spans: 0
- Status: Empty project

**Project: dev-agent-lens**
- Total spans: 1,000+ (limit reached)
- Time range: Recent data from today (2025-12-16)
- Columns: Complete Phoenix schema with all attributes
- Data quality: Good (includes LLM spans, tool calls, metadata)

### 3. Historical Sync Test (7 days, 1-day batches)

**Command:**
```bash
uv run dal sync-historical --days 7 --batch-size 1 --backend phoenix-local
```

**Results:**
- Total spans: 32,049
- Batches completed: 6/7 (85.7%)
- Batches failed: 1/7
- Time elapsed: 127.1s

**Daily Breakdown:**
- 2025-12-15 to 2025-12-16: ❌ FAILED (Server disconnected)
- 2025-12-14 to 2025-12-15: ✅ 173 spans
- 2025-12-13 to 2025-12-14: ⚪ No data
- 2025-12-12 to 2025-12-13: ✅ 3,125 spans
- 2025-12-11 to 2025-12-12: ✅ 20,029 spans (largest batch)
- 2025-12-10 to 2025-12-11: ✅ 1,074 spans
- 2025-12-09 to 2025-12-10: ✅ 7,648 spans

### 4. Historical Sync Test (30 days, 2-day batches)

**Command:**
```bash
uv run dal sync-historical --days 30 --batch-size 2 --backend phoenix-local
```

**Results:**
- Total spans: 13,659
- Batches completed: 10/15 (66.7%)
- Batches failed: 5/15 (33.3%)
- Time elapsed: 387.7s (6.5 minutes)

**Batch Breakdown:**
- Batch 1 (2025-12-14 to 2025-12-16): ❌ FAILED - Server disconnected
- Batch 2 (2025-12-12 to 2025-12-14): ✅ 3,125 spans
- Batch 3 (2025-12-10 to 2025-12-12): ❌ FAILED - Incomplete chunked read
- Batch 4 (2025-12-08 to 2025-12-10): ❌ FAILED - Timed out
- Batch 5 (2025-12-06 to 2025-12-08): ❌ FAILED - Server disconnected
- Batch 6 (2025-12-04 to 2025-12-06): ✅ 7,933 spans
- Batch 7 (2025-12-02 to 2025-12-04): ❌ FAILED - Timed out
- Batch 8 (2025-11-30 to 2025-12-02): ✅ 2,018 spans
- Batch 9-15 (2025-11-16 to 2025-11-30): ⚪ Mostly no data, 583 spans on 2025-11-24

**Error Patterns:**
- "Server disconnected without sending a response"
- "peer closed connection without sending complete message body (incomplete chunked read)"
- "timed out"

## Data Storage

### Sync State
- Last Phoenix sync: 2025-12-16T09:30:36.931714
- Last Arize sync: 2025-12-16T09:39:14.894214

### Raw Data Files
- Total size: 15GB
- Total files: 27
- Format: JSONL (newline-delimited JSON)
- Location: `~/.dal/data/raw/`

### Sample Data Structure
Phoenix spans are normalized and stored with fields:
- `span_id`, `trace_id`, `parent_id`
- `name`, `span_kind`, `start_time`, `end_time`
- `status_code`, `status_message`
- `input_value`, `output_value`
- `llm_model_name`, `llm_token_count_*`
- `backend`: "phoenix"
- `raw_attributes`: Complete Phoenix schema

## Issues Identified

### 1. Timeout Issues (Critical)
**Symptoms:**
- Random batch failures with connection resets
- "Server disconnected without sending a response"
- "Incomplete chunked read" errors
- Timeouts on larger date ranges

**Frequency:**
- 33% failure rate on 2-day batches (30-day test)
- 14% failure rate on 1-day batches (7-day test)
- Appears more common on recent dates (today's data)

**Impact:**
- Historical sync is unreliable for large datasets
- Requires manual retries to get complete data
- Missing data from failed batches

### 2. Phoenix Container Health
**Status:** Unhealthy
- Container reports unhealthy status despite being functional
- May indicate resource constraints or internal issues
- Could be contributing to timeout problems

### 3. Data Distribution
**Observations:**
- Data is not evenly distributed across dates
- Some days have 20,000+ spans, others have zero
- Most data is from December 2025 (last 2 weeks)
- Very little data before November 24, 2025

## Comparison: Phoenix vs Arize

### Phoenix Local (This Test)
- ✅ Free, self-hosted
- ✅ Fast when working (no API rate limits)
- ⚠️ Timeout issues on large queries
- ⚠️ Container health issues
- ⚠️ Less reliable for historical backfill
- ✅ 32,049 spans synced (7 days, 85% success)

### Arize Cloud (Previous Session)
- ✅ Cloud-hosted, no infrastructure
- ✅ More reliable for large queries
- ⚠️ API rate limits
- ✅ Currently running: 3.2M spans, 365 days
- ✅ Better for production/long-term storage

## Recommendations

### Short-term Fixes
1. **Reduce batch size:** Use 1-day batches instead of 2-day for better reliability
2. **Increase timeout:** Set longer client timeout (60s+ instead of 30s)
3. **Retry logic:** The sync already has retry logic (3 attempts), which is good
4. **Monitor Phoenix health:** Check Phoenix container logs and resource usage

### Long-term Improvements
1. **Investigate Phoenix timeouts:**
   - Check Phoenix container resources (CPU, memory)
   - Review Phoenix logs for errors
   - Consider upgrading Phoenix version
   - Check if Phoenix database needs optimization

2. **Hybrid approach:**
   - Use Phoenix for recent data (last 7 days)
   - Use Arize for historical backfill (30+ days)
   - Combine both for complete dataset

3. **Batch optimization:**
   - Adaptive batch sizing based on span density
   - Smaller batches for data-heavy periods
   - Larger batches for sparse periods

## Data Availability Summary

### Phoenix Local
- **Time range:** ~30 days (mostly last 2 weeks)
- **Span count:** 32,049+ (partial, with failures)
- **Data density:** High in December, sparse in November
- **Projects:** "dev-agent-lens" (active), "default" (empty)

### Successful Sync Dates
- 2025-12-11 to 2025-12-12: ✅ 20,029 spans
- 2025-12-09 to 2025-12-10: ✅ 7,648 spans
- 2025-12-04 to 2025-12-06: ✅ 7,933 spans
- 2025-12-12 to 2025-12-13: ✅ 3,125 spans
- 2025-11-30 to 2025-12-02: ✅ 2,018 spans

### Failed/Missing Dates
- 2025-12-14 to 2025-12-16: ❌ Failed (most recent)
- 2025-12-10 to 2025-12-12: ❌ Failed
- 2025-12-08 to 2025-12-10: ❌ Failed
- 2025-12-06 to 2025-12-08: ❌ Failed
- 2025-12-02 to 2025-12-04: ❌ Failed
- 2025-11-16 to 2025-11-24: ⚪ No data (except 583 spans on 11/24)

## Conclusion

Phoenix historical sync is **partially functional** but has significant reliability issues:

### ✅ What Works
- Phoenix client and connection
- Small batch fetching (1-day batches, 85%+ success)
- Data normalization and storage
- Retry logic for failed batches

### ⚠️ What Needs Improvement
- Timeout/connection issues (33% failure on 2-day batches)
- Phoenix container health
- Large date range queries
- Recent data queries (today's data fails most often)

### 📊 Data Quality
- Successfully synced 32,049+ spans from last 7 days
- Data is complete for working batches
- Schema and normalization work correctly
- Storage and state tracking functional

### 🔄 Comparison to Arize
- Arize is more reliable for large historical backfills
- Phoenix is faster when working (no rate limits)
- Phoenix is better for recent/incremental syncs
- Consider using both backends based on use case

## Next Steps

1. **Investigate Phoenix timeout root cause**
2. **Test with increased client timeout (60s+)**
3. **Re-run failed batches individually**
4. **Consider Phoenix container resource allocation**
5. **Document optimal batch sizes for Phoenix vs Arize**

---

**Test completed:** 2025-12-16 11:54:46
**Tester:** Claude Code
**Session:** Phoenix historical sync testing
