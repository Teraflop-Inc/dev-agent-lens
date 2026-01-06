# Parquet Export Optimization Guide

## Overview

This document describes the compression and optimization strategies implemented for exporting unified session data to Parquet format. These optimizations achieve **96-97% compression** (52 GB JSONL → ~1.8 GB Parquet).

## Implemented Strategies

### 1. ZSTD Compression (ENG2-642)

**Default compression changed from Snappy to ZSTD.**

ZSTD provides ~45% better compression than Snappy with similar read performance. At compression level 10-12, ZSTD matches Brotli's compression ratio but with faster read/write times.

```python
# In ParquetExporter.__init__
compression: str = "zstd"  # Changed from "snappy"
```

### 2. Dictionary Encoding (ENG2-643)

**Enabled for columns with high value duplication.**

Analysis revealed extreme value duplication in span data:
- `output_messages`: 1.83M rows → 2,235 unique values (819x duplication)
- `output_value`: 488K rows → 3,292 unique values (148x duplication)
- `input_value`: 494K rows → 4,635 unique values (107x duplication)

Top repeated values:
- "Todos have been modified successfully..." - 125,000+ occurrences
- Empty strings - 83,000+ occurrences
- "The user doesn't want to proceed..." - 18,700+ occurrences

Dictionary encoding stores each unique value once and uses small integer IDs for references.

```python
# Columns with dictionary encoding enabled
DICTIONARY_COLUMNS = [
    "input_value",
    "output_value",
    "input_messages",
    "output_messages",
    "raw_attributes_json",
    "name",
    "span_kind",
    "status_code",
    "llm_model_name",
    "source",
]
```

### 3. Field Deduplication (ENG2-639)

**Removes duplicate fields from `raw_attributes` that already exist in normalized columns.**

Fields removed from raw_attributes:
- `context.span_id` (duplicates `span_id`)
- `context.trace_id` (duplicates `trace_id`)
- `attributes.llm.model_name` (duplicates `llm_model_name`)
- `attributes.llm.input_messages` (duplicates `input_messages`)
- `attributes.llm.output_messages` (duplicates `output_messages`)
- `attributes.llm.token_count.*` (duplicates `llm_token_count_*`)
- And others...

See `dev_agent_lens/export/dedupe.py` for the full list in `DUPLICATED_FIELDS`.

### 4. Null/Empty Value Stripping (ENG2-640)

**Removes null, empty, and NaN values from raw_attributes.**

Values removed:
- `None`
- Empty strings (`""`)
- Empty lists (`[]`)
- Empty dicts (`{}`)
- NaN floats
- String `"nan"`

This is applied recursively to nested structures.

## Results

### arize-sightline (smallest file)

| Stage | Size | Reduction |
|-------|------|-----------|
| Original JSONL | 52.3 MB | - |
| Snappy + dedupe | 15.2 MB | 70.9% |
| **ZSTD + dictionary + dedupe** | **1.8 MB** | **96.6%** |

### Estimated for all files

| Source | JSONL | Parquet (est.) | Reduction |
|--------|-------|----------------|-----------|
| arize-sightline | 52 MB | 1.8 MB | 96.6% |
| phoenix-local-alex | 758 MB | ~25 MB | ~97% |
| phoenix-lambda2-dal | 25.8 GB | ~0.9 GB | ~97% |
| arize-ax-alex | 26.3 GB | ~0.9 GB | ~97% |
| **TOTAL** | **52.9 GB** | **~1.8 GB** | **~97%** |

## Usage

```bash
# Export with all optimizations (default)
dal export-parquet --source phoenix-local-alex

# Override compression
dal export-parquet --source phoenix-local-alex --compression snappy

# Disable optimizations (not recommended)
dal export-parquet --source phoenix-local-alex --no-dedupe --no-strip-nulls
```

## Data Flow

```
Phoenix/Arize Backend
        │
        ▼
   dal sync / dal sync-historical
        │
        ▼
   ~/.dal/data/unified/{source}_sessions.jsonl   (LARGE)
        │
        ▼
   dal export-parquet --source {source}
        │
        ▼
   ~/.dal/data/parquet/{source}_sessions.parquet  (SMALL)
   ~/.dal/data/parquet/{source}_spans.parquet
```

## Two-Table Design

The export creates two Parquet files per source:

### sessions.parquet (tiny)
Pre-computed aggregates for fast dashboard queries:
- `session_id`
- `source`
- `span_count`
- `first_span_time`, `last_span_time`
- `total_prompt_tokens`, `total_completion_tokens`, `total_tokens`
- `models_used`
- `has_errors`

### spans.parquet (larger)
Individual spans with full content:
- All span fields (span_id, trace_id, name, timestamps, etc.)
- Content fields (input_value, output_value, input_messages, output_messages)
- LLM metadata (model name, token counts)
- `raw_attributes_json` (deduplicated, as JSON string)

This separation allows fast session-level queries without scanning gigabytes of span content.

## Future Optimization Opportunities

These are NOT implemented but could provide marginal additional savings:

| Strategy | Effort | Potential Impact |
|----------|--------|------------------|
| Remove constant fields from raw_attributes | Low | ~10-15% |
| Remove computable fields (e.g., `latency_ms`) | Low | ~5-10% |
| Higher ZSTD compression level (10-12) | Trivial | ~5-10% |
| Row group size tuning | Low | Variable |

At 96.6% compression, these provide diminishing returns.

## Verification

To verify dictionary encoding is applied:

```python
import pyarrow.parquet as pq

path = '~/.dal/data/parquet/arize-sightline_spans.parquet'
pf = pq.ParquetFile(path)
meta = pf.metadata

print(f'Compression: {meta.row_group(0).column(0).compression}')

# Check encodings
schema = pf.schema_arrow
for i in range(meta.num_columns):
    col_meta = meta.row_group(0).column(i)
    col_name = schema.field(i).name
    enc_str = str(col_meta.encodings)
    has_dict = 'DICTIONARY' in enc_str or 'RLE' in enc_str
    print(f'{col_name}: {"DICTIONARY" if has_dict else "PLAIN"}')
```

Expected output shows `RLE_DICTIONARY` for text columns.

## Related Linear Issues

- ENG2-639: Deduplicate Messages at Export Time
- ENG2-640: Strip Null/Empty Values from raw_attributes
- ENG2-641: Parquet Export for Unified Sessions
- ENG2-642: Switch Parquet compression from Snappy to ZSTD
- ENG2-643: Enable Parquet dictionary encoding for high-duplication columns
