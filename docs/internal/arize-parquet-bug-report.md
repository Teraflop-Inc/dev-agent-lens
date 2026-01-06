# Arize SDK Bug Report: export_model_to_parquet fails with PyArrow schema error

## Summary

The `ArizeExportClient.export_model_to_parquet()` method fails with a PyArrow schema validation error when exporting tracing data. The same data exports successfully using `export_model_to_df()`.

## Environment

- **arize SDK version**: Latest (installed via pip)
- **Python version**: 3.13
- **PyArrow version**: (bundled with arize SDK)
- **OS**: macOS Darwin 24.6.0

## Error Message

```
pyarrow.lib.ArrowInvalid: Column 'attributes.llm.invocation_parameters' is declared non-nullable but contains nulls
```

## Full Stack Trace

```
Traceback (most recent call last):
  File "<stdin>", line 43, in <module>
  File "/path/to/.venv/lib/python3.13/site-packages/arize/exporter/core/client.py", line 278, in export_model_to_parquet
    writer.write_batch(record_batch)
  File "/path/to/.venv/lib/python3.13/site-packages/pyarrow/parquet/core.py", line 1139, in write_batch
    self.write_table(table, row_group_size)
  File "/path/to/.venv/lib/python3.13/site-packages/pyarrow/parquet/core.py", line 1166, in write_table
    self.writer.write_table(table, row_group_size=row_group_size)
  File "pyarrow/_parquet.pyx", line 2386, in pyarrow._parquet.ParquetWriter.write_table
  File "pyarrow/error.pxi", line 92, in pyarrow.lib.check_status
pyarrow.lib.ArrowInvalid: Column 'attributes.llm.invocation_parameters' is declared non-nullable but contains nulls
```

## Steps to Reproduce

```python
import os
from datetime import datetime, timedelta
from arize.exporter import ArizeExportClient
from arize.utils.types import Environments

# Initialize client
client = ArizeExportClient(api_key=os.environ["ARIZE_API_KEY"])

# Define export parameters
space_id = os.environ["ARIZE_SPACE_KEY"]
model_id = "dev-agent-lens"  # Any model with tracing data
start_time = datetime.now() - timedelta(days=30)
end_time = datetime.now()

# This works fine - returns 7355 rows
df = client.export_model_to_df(
    space_id=space_id,
    model_id=model_id,
    environment=Environments.TRACING,
    start_time=start_time,
    end_time=end_time,
)
print(f"DataFrame export: {len(df)} rows")  # Success: 7355 rows

# This fails with PyArrow error
result = client.export_model_to_parquet(
    path="/tmp/export.parquet",
    space_id=space_id,
    model_id=model_id,
    environment=Environments.TRACING,
    start_time=start_time,
    end_time=end_time,
)
# Raises: pyarrow.lib.ArrowInvalid
```

## Expected Behavior

`export_model_to_parquet()` should successfully write the data to a parquet file, similar to how `export_model_to_df()` successfully returns a DataFrame.

## Actual Behavior

The export fails at the PyArrow level because the parquet schema declares `attributes.llm.invocation_parameters` (and possibly other columns) as non-nullable, but the actual data contains null values.

## Root Cause Analysis

The issue appears to be in the schema definition used when creating the parquet writer. The schema declares certain columns as non-nullable (`nullable=False`), but the actual tracing data contains null values in those columns.

Potential fix approaches:
1. Update the parquet schema to mark these columns as nullable
2. Fill null values with empty strings/objects before writing
3. Dynamically infer nullability from the actual data

## Workaround

Currently using `export_model_to_df()` followed by manual parquet conversion:

```python
df = client.export_model_to_df(...)
df.to_parquet("/tmp/export.parquet")
```

However, this defeats the purpose of streaming directly to parquet for large datasets.

## Additional Context

- The data being exported is OpenTelemetry tracing spans from Claude Code sessions
- The `attributes.llm.invocation_parameters` column contains LLM invocation parameters which are not present on all span types (hence the nulls)
- Export progress shows the data streaming successfully until it hits the null value:
  ```
  exporting 7355 rows: 100%|███████████████████| 7355/7355 [00:12, 570.12 row/s]
  ```
  Then fails on the write_batch call

## Impact

This bug prevents using the more memory-efficient parquet streaming export for large datasets. Users are forced to load entire datasets into memory via DataFrame export, which may cause issues for very large trace exports.

---

**Submitted by**: Solutions Fabric Team
**Date**: 2025-12-16
**Related**: Arize AX tracing data export
