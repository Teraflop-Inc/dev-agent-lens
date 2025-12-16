# Unified Schema Documentation

This document describes the unified schema used by `dev_agent_lens` to normalize trace data from Phoenix and Arize backends into a consistent format.

## UnifiedSpan Schema

| Field | Type | Description |
|-------|------|-------------|
| `span_id` | `str` | Unique identifier for this span |
| `trace_id` | `str` | Identifier for the trace this span belongs to |
| `parent_id` | `str \| None` | ID of the parent span (None for root spans) |
| `name` | `str` | Name/label of the span |
| `span_kind` | `str \| None` | Type of span (LLM, TOOL, CHAIN, etc.) |
| `start_time` | `str` | ISO-8601 timestamp when span started |
| `end_time` | `str \| None` | ISO-8601 timestamp when span ended |
| `status_code` | `str \| None` | Status of the span (OK, ERROR, etc.) |
| `input_value` | `str \| None` | Input text/content for this span |
| `output_value` | `str \| None` | Output text/content from this span |
| `input_messages` | `str \| None` | Structured LLM input messages (JSON string) |
| `output_messages` | `str \| None` | Structured LLM output messages (JSON string) |
| `llm_model_name` | `str \| None` | Name of the LLM model used |
| `llm_token_count_prompt` | `int \| None` | Number of prompt tokens |
| `llm_token_count_completion` | `int \| None` | Number of completion tokens |
| `llm_token_count_total` | `int \| None` | Total token count |
| `backend` | `str` | Source backend ('phoenix' or 'arize') |
| `raw_attributes` | `str \| None` | Original attributes as JSON string |

## Field Mappings

### Phoenix → Unified

| Phoenix Column | Unified Field |
|---------------|---------------|
| `context.span_id` | `span_id` |
| `context.trace_id` | `trace_id` |
| `parent_id` | `parent_id` |
| `name` | `name` |
| `span_kind` | `span_kind` |
| `attributes.openinference.span.kind` | `span_kind` (fallback) |
| `start_time` | `start_time` (→ ISO-8601) |
| `end_time` | `end_time` (→ ISO-8601) |
| `status_code` / `status` | `status_code` |
| `attributes.input.value` | `input_value` |
| `attributes.output.value` | `output_value` |
| `attributes.llm.input_messages` | `input_messages` |
| `attributes.llm.output_messages` | `output_messages` |
| `attributes.llm.model_name` | `llm_model_name` |
| `attributes.llm.token_count.prompt` | `llm_token_count_prompt` |
| `attributes.llm.token_count.completion` | `llm_token_count_completion` |
| `attributes.llm.token_count.total` | `llm_token_count_total` |
| (derived) | `backend` = "phoenix" |

### Arize → Unified

| Arize Column | Unified Field |
|-------------|---------------|
| `context.span_id` | `span_id` |
| `context.trace_id` | `trace_id` |
| `parent_id` | `parent_id` |
| `name` | `name` |
| `attributes.openinference.span.kind` | `span_kind` |
| `start_time` | `start_time` (ms → ISO-8601) |
| `end_time` | `end_time` (ms → ISO-8601) |
| `status_code` / `status` | `status_code` |
| `attributes.input.value` | `input_value` |
| `attributes.output.value` | `output_value` |
| `attributes.llm.input_messages` | `input_messages` |
| `attributes.llm.output_messages` | `output_messages` |
| `attributes.llm.model_name` | `llm_model_name` |
| `attributes.llm.token_count.prompt` | `llm_token_count_prompt` |
| `attributes.llm.token_count.completion` | `llm_token_count_completion` |
| `attributes.llm.token_count.total` | `llm_token_count_total` |
| (derived) | `backend` = "arize" |

## Timestamp Normalization

All timestamps are normalized to ISO-8601 format:

- **Phoenix**: Timestamps may be `datetime` objects or pandas `Timestamp` → converted to ISO-8601 string
- **Arize**: Timestamps are typically milliseconds since epoch → converted to ISO-8601 string
- **ISO-8601 strings**: Preserved as-is
- **Null/NaN**: Converted to `None`

Example: `2025-01-15T10:30:00`

## Usage

```python
from dev_agent_lens.clients import PhoenixClient, ArizeClient
from dev_agent_lens.core import normalize_phoenix, normalize_arize

# From Phoenix
phoenix_client = PhoenixClient()
raw_df = phoenix_client.get_spans_dataframe()
unified_df = normalize_phoenix(raw_df)

# From Arize
arize_client = ArizeClient()
raw_df = arize_client.get_spans_dataframe()
unified_df = normalize_arize(raw_df)

# Both produce identical column structure
assert list(unified_df.columns) == UNIFIED_COLUMNS
```

## Notes

- Missing optional fields are set to `None`, not omitted
- The `backend` field allows identifying the source of data
- `raw_attributes` is reserved for storing original attributes if needed
- Token counts are converted to integers; invalid values become `None`
