"""
Batch Formatter Module (Story 4.1)

Formats trace spans into batches suitable for LLM processing.
Supports multiple batch formats and configurable sizing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dev_agent_lens.analysis.classify import SpanCategory, classify_span


@dataclass
class BatchConfig:
    """Configuration for batch formatting.

    Attributes:
        max_spans_per_batch: Maximum spans in a single batch
        max_tokens_estimate: Approximate token limit for batch content
        include_raw_attributes: Whether to include raw span attributes
        include_context: Whether to include surrounding context
        context_size: Number of context spans before/after
        format: Output format ('json', 'text', 'markdown')
    """

    max_spans_per_batch: int = 100
    max_tokens_estimate: int = 8000
    include_raw_attributes: bool = False
    include_context: bool = True
    context_size: int = 3
    format: str = "json"


@dataclass
class Batch:
    """A formatted batch of spans for LLM processing.

    Attributes:
        batch_id: Unique identifier for this batch
        session_id: Session ID if batch is from a single session
        spans: List of formatted span data
        span_count: Number of spans in the batch
        token_estimate: Estimated token count
        metadata: Additional batch metadata
    """

    batch_id: str
    session_id: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    span_count: int = 0
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "spans": self.spans,
            "span_count": self.span_count,
            "token_estimate": self.token_estimate,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_text(self) -> str:
        """Convert to plain text format for LLM consumption."""
        lines = []
        lines.append(f"=== Batch {self.batch_id} ===")
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        lines.append(f"Spans: {self.span_count}")
        lines.append("")

        for i, span in enumerate(self.spans):
            lines.append(f"--- Span {i + 1}: {span.get('name', 'unknown')} ---")
            lines.append(f"  Type: {span.get('category', 'unknown')}")
            if span.get("model"):
                lines.append(f"  Model: {span['model']}")
            if span.get("input"):
                # Truncate long inputs
                input_text = span["input"]
                if len(input_text) > 500:
                    input_text = input_text[:500] + "..."
                lines.append(f"  Input: {input_text}")
            if span.get("output"):
                output_text = span["output"]
                if len(output_text) > 500:
                    output_text = output_text[:500] + "..."
                lines.append(f"  Output: {output_text}")
            if span.get("status"):
                lines.append(f"  Status: {span['status']}")
            if span.get("duration_ms"):
                lines.append(f"  Duration: {span['duration_ms']:.0f}ms")
            lines.append("")

        return "\n".join(lines)

    def to_markdown(self) -> str:
        """Convert to markdown format for LLM consumption."""
        lines = []
        lines.append(f"# Batch {self.batch_id}")
        if self.session_id:
            lines.append(f"**Session:** `{self.session_id}`")
        lines.append(f"**Spans:** {self.span_count}")
        lines.append("")

        for i, span in enumerate(self.spans):
            lines.append(f"## Span {i + 1}: {span.get('name', 'unknown')}")
            lines.append("")
            lines.append(f"- **Type:** {span.get('category', 'unknown')}")
            if span.get("model"):
                lines.append(f"- **Model:** {span['model']}")
            if span.get("status"):
                lines.append(f"- **Status:** {span['status']}")
            if span.get("duration_ms"):
                lines.append(f"- **Duration:** {span['duration_ms']:.0f}ms")

            if span.get("input"):
                lines.append("")
                lines.append("**Input:**")
                lines.append("```")
                input_text = span["input"]
                if len(input_text) > 1000:
                    input_text = input_text[:1000] + "\n... (truncated)"
                lines.append(input_text)
                lines.append("```")

            if span.get("output"):
                lines.append("")
                lines.append("**Output:**")
                lines.append("```")
                output_text = span["output"]
                if len(output_text) > 1000:
                    output_text = output_text[:1000] + "\n... (truncated)"
                lines.append(output_text)
                lines.append("```")

            lines.append("")

        return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Estimate token count for text (rough approximation: ~4 chars per token)."""
    if not text:
        return 0
    return len(text) // 4


def _format_span(
    span: dict[str, Any],
    config: BatchConfig,
) -> dict[str, Any]:
    """Format a single span for LLM consumption.

    Args:
        span: Raw span dictionary
        config: Batch configuration

    Returns:
        Formatted span dictionary
    """
    # Classify the span
    classification = classify_span(span)

    # Calculate duration
    duration_ms = None
    start_time = span.get("start_time")
    end_time = span.get("end_time")
    if start_time and end_time:
        try:
            from datetime import datetime

            start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_ms = (end - start).total_seconds() * 1000
        except (ValueError, TypeError):
            pass

    formatted = {
        "span_id": span.get("span_id"),
        "name": span.get("name"),
        "category": classification.category.value,
        "confidence": classification.confidence,
        "status": span.get("status_code"),
        "model": span.get("llm_model_name"),
        "input": span.get("input_value"),
        "output": span.get("output_value"),
        "tokens_prompt": span.get("llm_token_count_prompt"),
        "tokens_completion": span.get("llm_token_count_completion"),
        "duration_ms": duration_ms,
        "start_time": start_time,
        "end_time": end_time,
    }

    if config.include_raw_attributes:
        formatted["raw_attributes"] = span.get("raw_attributes")

    # Remove None values for cleaner output
    return {k: v for k, v in formatted.items() if v is not None}


def format_spans_for_llm(
    spans: list[dict[str, Any]],
    config: BatchConfig | None = None,
) -> list[dict[str, Any]]:
    """Format a list of spans for LLM consumption.

    Args:
        spans: List of raw span dictionaries
        config: Batch configuration (uses defaults if None)

    Returns:
        List of formatted span dictionaries
    """
    if config is None:
        config = BatchConfig()

    return [_format_span(span, config) for span in spans]


def format_batch(
    spans: list[dict[str, Any]],
    batch_id: str = "batch_0",
    session_id: str | None = None,
    config: BatchConfig | None = None,
) -> Batch:
    """Format spans into a single batch.

    Args:
        spans: List of raw span dictionaries
        batch_id: Identifier for this batch
        session_id: Optional session identifier
        config: Batch configuration (uses defaults if None)

    Returns:
        Formatted Batch object
    """
    if config is None:
        config = BatchConfig()

    if not spans:
        return Batch(
            batch_id=batch_id,
            session_id=session_id,
            spans=[],
            span_count=0,
            token_estimate=0,
        )

    # Format spans
    formatted_spans = format_spans_for_llm(spans, config)

    # Estimate tokens
    total_tokens = 0
    for span in formatted_spans:
        # Estimate based on input/output content
        total_tokens += _estimate_tokens(str(span.get("input", "")))
        total_tokens += _estimate_tokens(str(span.get("output", "")))
        total_tokens += 50  # Base overhead per span

    # Calculate metadata
    categories: dict[str, int] = {}
    for span in formatted_spans:
        cat = span.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    models: dict[str, int] = {}
    for span in formatted_spans:
        model = span.get("model")
        if model:
            models[model] = models.get(model, 0) + 1

    metadata = {
        "categories": categories,
        "models": models,
        "has_errors": any(span.get("status") == "ERROR" for span in formatted_spans),
    }

    return Batch(
        batch_id=batch_id,
        session_id=session_id,
        spans=formatted_spans,
        span_count=len(formatted_spans),
        token_estimate=total_tokens,
        metadata=metadata,
    )


def format_session_batch(
    session: dict[str, Any],
    batch_id: str | None = None,
    config: BatchConfig | None = None,
) -> Batch:
    """Format a session into a batch.

    Args:
        session: Session dictionary with session_id and spans
        batch_id: Optional batch identifier (uses session_id if None)
        config: Batch configuration

    Returns:
        Formatted Batch object
    """
    session_id = session.get("session_id")
    spans = session.get("spans", [])

    if batch_id is None:
        batch_id = f"session_{session_id}" if session_id else "batch_0"

    return format_batch(
        spans=spans,
        batch_id=batch_id,
        session_id=session_id,
        config=config,
    )


def create_batches(
    spans: list[dict[str, Any]],
    config: BatchConfig | None = None,
) -> list[Batch]:
    """Split spans into multiple batches based on configuration limits.

    Args:
        spans: List of raw span dictionaries
        config: Batch configuration

    Returns:
        List of Batch objects
    """
    if config is None:
        config = BatchConfig()

    if not spans:
        return []

    batches = []
    current_spans: list[dict[str, Any]] = []
    current_tokens = 0
    batch_num = 0

    for span in spans:
        formatted = _format_span(span, config)
        span_tokens = (
            _estimate_tokens(str(formatted.get("input", "")))
            + _estimate_tokens(str(formatted.get("output", "")))
            + 50
        )

        # Check if adding this span would exceed limits
        if (
            len(current_spans) >= config.max_spans_per_batch
            or current_tokens + span_tokens > config.max_tokens_estimate
        ):
            # Save current batch and start new one
            if current_spans:
                batch = format_batch(
                    current_spans,
                    batch_id=f"batch_{batch_num}",
                    config=config,
                )
                batches.append(batch)
                batch_num += 1

            current_spans = [span]
            current_tokens = span_tokens
        else:
            current_spans.append(span)
            current_tokens += span_tokens

    # Don't forget the last batch
    if current_spans:
        batch = format_batch(
            current_spans,
            batch_id=f"batch_{batch_num}",
            config=config,
        )
        batches.append(batch)

    return batches


def get_batch_summary(batch: Batch) -> dict[str, Any]:
    """Get a summary of a batch for quick inspection.

    Args:
        batch: Batch object

    Returns:
        Summary dictionary
    """
    return {
        "batch_id": batch.batch_id,
        "session_id": batch.session_id,
        "span_count": batch.span_count,
        "token_estimate": batch.token_estimate,
        "categories": batch.metadata.get("categories", {}),
        "models": batch.metadata.get("models", {}),
        "has_errors": batch.metadata.get("has_errors", False),
    }


# Fields to query from Parquet for batch formatting (minimizes memory)
PARQUET_BATCH_FIELDS = [
    "session_id",
    "span_id",
    "name",
    "span_kind",
    "status_code",
    "input_value",
    "output_value",
    "llm_model_name",
    "llm_token_count_prompt",
    "llm_token_count_completion",
    "start_time",
    "end_time",
]


def format_batch_from_parquet(
    spans_path: str,
    session_ids: list[str],
    config: BatchConfig | None = None,
) -> list[Batch]:
    """Create batches from Parquet data with optimized field selection.

    This function queries only the fields needed for batch formatting,
    reducing memory usage by 50%+ compared to loading full sessions.

    Args:
        spans_path: Path to the spans Parquet file
        session_ids: List of session IDs to include in batches
        config: Batch configuration (uses defaults if None)

    Returns:
        List of formatted Batch objects, one per session

    Raises:
        ImportError: If DuckDB is not available

    Example:
        >>> batches = format_batch_from_parquet(
        ...     "~/.dal/data/parquet/my-project_spans.parquet",
        ...     ["session_abc", "session_xyz"],
        ... )
        >>> for batch in batches:
        ...     print(f"Session {batch.session_id}: {batch.span_count} spans")
    """
    try:
        import duckdb
    except ImportError as e:
        raise ImportError(
            "DuckDB is required for Parquet batch formatting. "
            "Install with: pip install duckdb"
        ) from e

    if config is None:
        config = BatchConfig()

    if not session_ids:
        return []

    # Build query with only needed fields
    fields_str = ", ".join(PARQUET_BATCH_FIELDS)
    session_ids_quoted = ", ".join(f"'{s}'" for s in session_ids)

    query = f"""
        SELECT {fields_str}
        FROM read_parquet('{spans_path}')
        WHERE session_id IN ({session_ids_quoted})
        ORDER BY session_id, start_time
    """

    con = duckdb.connect()
    df = con.execute(query).df()
    con.close()

    if df.empty:
        return []

    # Group by session and format
    batches = []
    for session_id, group in df.groupby("session_id"):
        spans = group.to_dict("records")
        batch = format_batch(
            spans=spans,
            batch_id=f"session_{session_id}",
            session_id=str(session_id),
            config=config,
        )
        batches.append(batch)

    return batches


def create_batches_from_parquet(
    spans_path: str,
    session_ids: list[str] | None = None,
    limit: int | None = None,
    config: BatchConfig | None = None,
) -> list[Batch]:
    """Create batches from Parquet with optional filtering.

    Convenience wrapper around format_batch_from_parquet that supports
    querying all sessions or a limited subset.

    Args:
        spans_path: Path to the spans Parquet file
        session_ids: Optional list of session IDs. If None, queries all sessions.
        limit: Maximum number of sessions to process
        config: Batch configuration

    Returns:
        List of formatted Batch objects
    """
    try:
        import duckdb
    except ImportError as e:
        raise ImportError(
            "DuckDB is required for Parquet batch formatting. "
            "Install with: pip install duckdb"
        ) from e

    # If no session_ids provided, get them from the file
    if session_ids is None:
        con = duckdb.connect()
        limit_clause = f"LIMIT {limit}" if limit else ""
        query = f"""
            SELECT DISTINCT session_id
            FROM read_parquet('{spans_path}')
            WHERE session_id IS NOT NULL
            ORDER BY session_id
            {limit_clause}
        """
        result = con.execute(query).fetchall()
        con.close()
        session_ids = [row[0] for row in result]

    # Apply limit if session_ids was provided
    if limit and len(session_ids) > limit:
        session_ids = session_ids[:limit]

    return format_batch_from_parquet(spans_path, session_ids, config)
