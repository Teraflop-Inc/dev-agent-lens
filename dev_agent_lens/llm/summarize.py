"""
Session Summarization Module (Story 4.3)

Generates LLM-powered summaries of trace sessions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dev_agent_lens.analysis.sessions import session_metrics
from dev_agent_lens.llm.batch import Batch, BatchConfig, format_batch
from dev_agent_lens.llm.prompts import PromptType, load_prompt, render_prompt
from dev_agent_lens.llm.router import (
    AnalysisType,
    LLMResponse,
    NoLLMConfigError,
    get_llm_config,
    route_request,
)


@dataclass
class SessionSummary:
    """Summary of a trace session.

    Attributes:
        session_id: Session identifier
        summary: The generated summary text
        span_count: Number of spans in the session
        duration_minutes: Session duration in minutes
        tool_count: Number of tool calls
        failure_count: Number of failures
        tokens_used: Tokens used for generation
        model_used: Model used for generation
    """

    session_id: str | None
    summary: str
    span_count: int = 0
    duration_minutes: float = 0.0
    tool_count: int = 0
    failure_count: int = 0
    tokens_used: dict[str, int] = field(default_factory=dict)
    model_used: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "span_count": self.span_count,
            "duration_minutes": self.duration_minutes,
            "tool_count": self.tool_count,
            "failure_count": self.failure_count,
            "tokens_used": self.tokens_used,
            "model_used": self.model_used,
        }


def _format_duration(minutes: float) -> str:
    """Format duration as human-readable string."""
    if minutes < 1:
        return f"{minutes * 60:.0f} seconds"
    elif minutes < 60:
        return f"{minutes:.1f} minutes"
    else:
        hours = minutes / 60
        return f"{hours:.1f} hours"


async def summarize_session(
    session: dict[str, Any],
    model: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
) -> SessionSummary:
    """Generate a summary for a session.

    Args:
        session: Session dictionary with session_id and spans
        model: Optional model override
        prompt_file: Optional custom prompt file
        prompt_override: Optional inline prompt override

    Returns:
        SessionSummary with generated summary

    Raises:
        NoLLMConfigError: If no LLM is configured
    """
    session_id = session.get("session_id")
    spans = session.get("spans", [])

    if not spans:
        return SessionSummary(
            session_id=session_id,
            summary="Empty session - no spans to analyze.",
            span_count=0,
        )

    # Compute metrics
    metrics = session_metrics(spans, session_id=session_id)

    # Format batch for LLM
    config = BatchConfig(
        max_spans_per_batch=200,
        max_tokens_estimate=6000,
        include_raw_attributes=False,
    )
    batch = format_batch(spans, session_id=session_id, config=config)

    # Load prompt
    template = load_prompt(
        PromptType.SUMMARIZE,
        prompt_file=prompt_file,
        prompt_override=prompt_override,
    )

    # Prepare variables
    variables = {
        "session_id": session_id or "unknown",
        "span_count": batch.span_count,
        "duration": _format_duration(metrics.duration_minutes),
        "session_data": batch.to_text(),
    }

    # Render prompt
    prompt = render_prompt(template, variables)

    # Get LLM config
    llm_config = get_llm_config(
        AnalysisType.SUMMARIZE,
        model=model,
    )

    # Generate summary
    response = await route_request(
        prompt=prompt,
        config=llm_config,
        system_prompt="You are a trace analysis assistant. Analyze the provided session data and generate concise, actionable summaries.",
    )

    return SessionSummary(
        session_id=session_id,
        summary=response.content,
        span_count=metrics.span_count,
        duration_minutes=metrics.duration_minutes,
        tool_count=metrics.tool_call_count,
        failure_count=metrics.failure_count,
        tokens_used=response.usage,
        model_used=response.model,
    )


async def summarize_sessions_batch(
    sessions: list[dict[str, Any]],
    model: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
    max_concurrent: int = 3,
) -> list[SessionSummary]:
    """Generate summaries for multiple sessions.

    Args:
        sessions: List of session dictionaries
        model: Optional model override
        prompt_file: Optional custom prompt file
        prompt_override: Optional inline prompt override
        max_concurrent: Maximum concurrent API calls

    Returns:
        List of SessionSummary objects
    """
    if not sessions:
        return []

    # Use semaphore to limit concurrency
    semaphore = asyncio.Semaphore(max_concurrent)

    async def summarize_with_limit(session: dict[str, Any]) -> SessionSummary:
        async with semaphore:
            return await summarize_session(
                session,
                model=model,
                prompt_file=prompt_file,
                prompt_override=prompt_override,
            )

    tasks = [summarize_with_limit(s) for s in sessions]
    return await asyncio.gather(*tasks)


def summarize_session_sync(
    session: dict[str, Any],
    model: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
) -> SessionSummary:
    """Synchronous wrapper for summarize_session.

    Args:
        session: Session dictionary with session_id and spans
        model: Optional model override
        prompt_file: Optional custom prompt file
        prompt_override: Optional inline prompt override

    Returns:
        SessionSummary with generated summary
    """
    return asyncio.run(
        summarize_session(
            session,
            model=model,
            prompt_file=prompt_file,
            prompt_override=prompt_override,
        )
    )


def get_summary_preview(
    session: dict[str, Any],
) -> dict[str, Any]:
    """Get a preview of what would be sent to the LLM.

    Useful for debugging or cost estimation without making API calls.

    Args:
        session: Session dictionary

    Returns:
        Dictionary with prompt preview and metadata
    """
    session_id = session.get("session_id")
    spans = session.get("spans", [])

    # Compute metrics
    metrics = session_metrics(spans, session_id=session_id)

    # Format batch
    config = BatchConfig(max_spans_per_batch=200, max_tokens_estimate=6000)
    batch = format_batch(spans, session_id=session_id, config=config)

    # Load default prompt
    template = load_prompt(PromptType.SUMMARIZE)

    # Prepare variables
    variables = {
        "session_id": session_id or "unknown",
        "span_count": batch.span_count,
        "duration": _format_duration(metrics.duration_minutes),
        "session_data": batch.to_text(),
    }

    # Render prompt
    prompt = render_prompt(template, variables)

    return {
        "session_id": session_id,
        "span_count": len(spans),
        "prompt_length": len(prompt),
        "estimated_tokens": len(prompt) // 4,
        "batch_summary": {
            "categories": batch.metadata.get("categories", {}),
            "models": batch.metadata.get("models", {}),
            "has_errors": batch.metadata.get("has_errors", False),
        },
        "metrics": {
            "duration_minutes": metrics.duration_minutes,
            "tool_calls": metrics.tool_call_count,
            "failures": metrics.failure_count,
        },
    }
