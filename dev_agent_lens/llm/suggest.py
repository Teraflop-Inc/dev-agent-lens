"""
Improvement Suggestions Module (Story 4.5)

Generates LLM-powered suggestions for improving agent sessions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dev_agent_lens.analysis.churn import detect_churn
from dev_agent_lens.analysis.failures import detect_failures, get_failure_summary
from dev_agent_lens.analysis.sessions import session_metrics
from dev_agent_lens.llm.batch import BatchConfig, format_batch
from dev_agent_lens.llm.prompts import PromptType, load_prompt, render_prompt
from dev_agent_lens.llm.router import (
    AnalysisType,
    NoLLMConfigError,
    get_llm_config,
    route_request,
)


class SuggestionCategory(str, Enum):
    """Categories of improvement suggestions."""

    ERROR = "error"
    EFFICIENCY = "efficiency"
    CHURN = "churn"
    BEST_PRACTICE = "best_practice"
    PERFORMANCE = "performance"


class SuggestionSeverity(str, Enum):
    """Severity levels for suggestions."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Suggestion:
    """A single improvement suggestion.

    Attributes:
        category: Suggestion category
        severity: Severity level
        title: Short title for the suggestion
        description: Detailed description of the issue
        recommendation: Recommended action
        impact: Expected impact if addressed
        evidence: Supporting evidence from the session
    """

    category: SuggestionCategory
    severity: SuggestionSeverity
    title: str
    description: str
    recommendation: str
    impact: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "recommendation": self.recommendation,
            "impact": self.impact,
            "evidence": self.evidence,
        }


@dataclass
class SuggestionResult:
    """Result of suggestion generation.

    Attributes:
        session_id: Session identifier
        suggestions: List of suggestions
        summary: Overall summary
        tokens_used: Tokens used for generation
        model_used: Model used for generation
    """

    session_id: str | None
    suggestions: list[Suggestion] = field(default_factory=list)
    summary: str = ""
    tokens_used: dict[str, int] = field(default_factory=dict)
    model_used: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "session_id": self.session_id,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "summary": self.summary,
            "suggestion_count": len(self.suggestions),
            "by_severity": self._count_by_severity(),
            "by_category": self._count_by_category(),
            "tokens_used": self.tokens_used,
            "model_used": self.model_used,
        }

    def _count_by_severity(self) -> dict[str, int]:
        """Count suggestions by severity."""
        counts: dict[str, int] = {}
        for s in self.suggestions:
            key = s.severity.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _count_by_category(self) -> dict[str, int]:
        """Count suggestions by category."""
        counts: dict[str, int] = {}
        for s in self.suggestions:
            key = s.category.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def filter_by_severity(self, severity: SuggestionSeverity) -> list[Suggestion]:
        """Get suggestions of a specific severity."""
        return [s for s in self.suggestions if s.severity == severity]

    def filter_by_category(self, category: SuggestionCategory) -> list[Suggestion]:
        """Get suggestions of a specific category."""
        return [s for s in self.suggestions if s.category == category]


def _generate_heuristic_suggestions(
    spans: list[dict[str, Any]],
    session_id: str | None,
) -> list[Suggestion]:
    """Generate suggestions based on heuristics without LLM.

    Args:
        spans: List of spans
        session_id: Session identifier

    Returns:
        List of heuristic-based suggestions
    """
    suggestions = []

    # Analyze failures
    failures = detect_failures(spans)
    failure_summary = get_failure_summary(failures)

    if failure_summary["total_failures"] > 0:
        by_type = failure_summary.get("by_type", {})

        if by_type.get("errors", 0) > 0:
            suggestions.append(Suggestion(
                category=SuggestionCategory.ERROR,
                severity=SuggestionSeverity.HIGH,
                title="Errors Detected",
                description=f"Session has {by_type['errors']} error(s) that should be investigated.",
                recommendation="Review error messages and fix underlying issues.",
                impact="Improved reliability and success rate.",
            ))

        if by_type.get("back_to_back", 0) > 0:
            suggestions.append(Suggestion(
                category=SuggestionCategory.EFFICIENCY,
                severity=SuggestionSeverity.MEDIUM,
                title="Repeated Operations",
                description=f"Found {by_type['back_to_back']} back-to-back identical operations.",
                recommendation="Avoid repeating the same operation consecutively.",
                impact="Reduced token usage and faster execution.",
            ))

        if by_type.get("rate_limits", 0) > 0:
            suggestions.append(Suggestion(
                category=SuggestionCategory.PERFORMANCE,
                severity=SuggestionSeverity.HIGH,
                title="Rate Limiting",
                description=f"Hit rate limits {by_type['rate_limits']} time(s).",
                recommendation="Implement backoff strategies or reduce request frequency.",
                impact="Smoother operation without interruptions.",
            ))

    # Analyze churn
    churn = detect_churn(spans)
    if churn.has_churn:
        if churn.multi_edit_files:
            suggestions.append(Suggestion(
                category=SuggestionCategory.CHURN,
                severity=SuggestionSeverity.MEDIUM,
                title="File Edit Churn",
                description=f"Files edited 3+ times: {', '.join(churn.multi_edit_files[:3])}",
                recommendation="Plan changes more carefully before editing files.",
                impact="Cleaner history and fewer wasted edits.",
                evidence=churn.multi_edit_files[:5],
            ))

        if churn.write_edit_files:
            suggestions.append(Suggestion(
                category=SuggestionCategory.CHURN,
                severity=SuggestionSeverity.LOW,
                title="Write-Then-Edit Pattern",
                description="Files were written then immediately edited.",
                recommendation="Write more complete content initially.",
                impact="Fewer operations and cleaner workflow.",
                evidence=churn.write_edit_files[:5],
            ))

    # Analyze metrics
    metrics = session_metrics(spans, session_id=session_id)

    if metrics.token_count_total > 100000:
        suggestions.append(Suggestion(
            category=SuggestionCategory.EFFICIENCY,
            severity=SuggestionSeverity.MEDIUM,
            title="High Token Usage",
            description=f"Session used {metrics.token_count_total:,} tokens.",
            recommendation="Consider breaking large tasks into smaller sessions.",
            impact="Lower costs and faster context processing.",
        ))

    return suggestions


async def suggest_improvements(
    session: dict[str, Any],
    model: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
    categories: list[str] | None = None,
) -> SuggestionResult:
    """Generate improvement suggestions for a session.

    Args:
        session: Session dictionary with session_id and spans
        model: Optional model override
        prompt_file: Optional custom prompt file
        prompt_override: Optional inline prompt override
        categories: Optional list of categories to focus on

    Returns:
        SuggestionResult with suggestions

    Raises:
        NoLLMConfigError: If no LLM is configured
    """
    session_id = session.get("session_id")
    spans = session.get("spans", [])

    if not spans:
        return SuggestionResult(
            session_id=session_id,
            summary="Empty session - no analysis possible.",
        )

    # Generate heuristic suggestions first
    heuristic_suggestions = _generate_heuristic_suggestions(spans, session_id)

    # Compute metrics for prompt
    metrics = session_metrics(spans, session_id=session_id)
    failures = detect_failures(spans)
    failure_summary = get_failure_summary(failures)

    # Format session data
    config = BatchConfig(
        max_spans_per_batch=200,
        max_tokens_estimate=5000,
    )
    batch = format_batch(spans, session_id=session_id, config=config)

    # Load prompt
    template = load_prompt(
        PromptType.SUGGEST,
        prompt_file=prompt_file,
        prompt_override=prompt_override,
    )

    # Format failure details
    failure_details = []
    for f in failures.failures[:10]:  # Limit to 10 failures
        failure_details.append(
            f"- {f.failure_type.value}: {f.reason} (span: {f.span.get('name')})"
        )

    # Prepare variables
    variables = {
        "session_id": session_id or "unknown",
        "duration": f"{metrics.duration_minutes:.1f} minutes",
        "tool_count": metrics.tool_call_count,
        "failure_count": failure_summary["total_failures"],
        "session_data": batch.to_text(),
        "failures": "\n".join(failure_details) if failure_details else "No failures detected.",
    }

    # Render prompt
    prompt = render_prompt(template, variables)

    # Get LLM config
    llm_config = get_llm_config(
        AnalysisType.SUGGEST,
        model=model,
    )

    # Generate suggestions
    response = await route_request(
        prompt=prompt,
        config=llm_config,
        system_prompt="You are a trace analysis assistant. Analyze sessions and provide actionable improvement suggestions.",
    )

    # Parse LLM response into suggestions
    llm_suggestions = _parse_suggestions(response.content)

    # Combine heuristic and LLM suggestions
    all_suggestions = heuristic_suggestions + llm_suggestions

    # Filter by category if specified
    if categories:
        category_set = {SuggestionCategory(c) for c in categories if c in [e.value for e in SuggestionCategory]}
        all_suggestions = [s for s in all_suggestions if s.category in category_set]

    # Sort by severity (high first)
    severity_order = {SuggestionSeverity.HIGH: 0, SuggestionSeverity.MEDIUM: 1, SuggestionSeverity.LOW: 2}
    all_suggestions.sort(key=lambda s: severity_order.get(s.severity, 3))

    return SuggestionResult(
        session_id=session_id,
        suggestions=all_suggestions,
        summary=response.content[:500] if len(response.content) > 500 else response.content,
        tokens_used=response.usage,
        model_used=response.model,
    )


def _parse_suggestions(llm_response: str) -> list[Suggestion]:
    """Parse LLM response into suggestion objects.

    Args:
        llm_response: Raw LLM response text

    Returns:
        List of parsed suggestions
    """
    suggestions = []

    # Simple parsing - look for numbered items or bullet points
    lines = llm_response.split("\n")
    current_suggestion: dict[str, Any] = {}

    def is_new_suggestion(line: str) -> bool:
        """Check if line starts a new suggestion (numbered or bullet)."""
        # Numbered items like "1." "2." etc
        if len(line) >= 2 and line[0].isdigit() and line[1] == ".":
            return True
        # Bullet points (but not markdown bold like **text**)
        if line.startswith("-") and not line.startswith("--"):
            return True
        if line.startswith("•"):
            return True
        # Asterisk bullet (but not bold markdown)
        if line.startswith("* ") and not line.startswith("**"):
            return True
        return False

    def is_field_line(line: str) -> bool:
        """Check if line is a field like Category: or Severity:."""
        lower = line.lower()
        return any(field in lower for field in [
            "category:", "severity:", "recommendation:", "action:",
            "impact:", "description:", "expected impact:"
        ])

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip markdown headers
        if line.startswith("#"):
            continue

        # Check for field lines first (before checking bullets)
        if is_field_line(line):
            lower = line.lower()
            if "severity:" in lower or "**severity" in lower:
                if "high" in lower:
                    current_suggestion["severity"] = SuggestionSeverity.HIGH
                elif "medium" in lower:
                    current_suggestion["severity"] = SuggestionSeverity.MEDIUM
                elif "low" in lower:
                    current_suggestion["severity"] = SuggestionSeverity.LOW

            elif "category:" in lower or "**category" in lower:
                for cat in SuggestionCategory:
                    if cat.value in lower:
                        current_suggestion["category"] = cat
                        break

            elif "recommendation:" in lower or "action:" in lower or "recommended action:" in lower:
                value = line.split(":", 1)[-1].strip()
                # Remove markdown bold markers
                value = value.replace("**", "").strip()
                current_suggestion["recommendation"] = value

            elif "impact:" in lower or "expected impact:" in lower:
                value = line.split(":", 1)[-1].strip()
                value = value.replace("**", "").strip()
                current_suggestion["impact"] = value

            elif "description:" in lower:
                value = line.split(":", 1)[-1].strip()
                value = value.replace("**", "").strip()
                if value:
                    current_suggestion["description"] = value

        # Check for new suggestion start
        elif is_new_suggestion(line):
            # Save previous suggestion if exists
            if current_suggestion.get("title"):
                suggestions.append(_create_suggestion_from_dict(current_suggestion))
                current_suggestion = {}

            # Start new suggestion
            text = line.lstrip("0123456789.-*• ").strip()
            # Remove markdown bold
            text = text.replace("**", "").strip()
            current_suggestion["title"] = text[:80]
            current_suggestion["description"] = text

    # Don't forget last suggestion
    if current_suggestion.get("title"):
        suggestions.append(_create_suggestion_from_dict(current_suggestion))

    return suggestions


def _create_suggestion_from_dict(data: dict[str, Any]) -> Suggestion:
    """Create Suggestion from parsed dictionary."""
    return Suggestion(
        category=data.get("category", SuggestionCategory.BEST_PRACTICE),
        severity=data.get("severity", SuggestionSeverity.MEDIUM),
        title=data.get("title", "Improvement suggestion"),
        description=data.get("description", ""),
        recommendation=data.get("recommendation", "Review and address this issue."),
        impact=data.get("impact", ""),
    )


def suggest_improvements_sync(
    session: dict[str, Any],
    model: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
    categories: list[str] | None = None,
) -> SuggestionResult:
    """Synchronous wrapper for suggest_improvements.

    Args:
        session: Session dictionary with session_id and spans
        model: Optional model override
        prompt_file: Optional custom prompt file
        prompt_override: Optional inline prompt override
        categories: Optional list of categories to focus on

    Returns:
        SuggestionResult with suggestions
    """
    return asyncio.run(
        suggest_improvements(
            session,
            model=model,
            prompt_file=prompt_file,
            prompt_override=prompt_override,
            categories=categories,
        )
    )


def get_suggestion_preview(
    session: dict[str, Any],
) -> dict[str, Any]:
    """Get a preview of suggestions without LLM calls.

    Returns heuristic-based suggestions only.

    Args:
        session: Session dictionary

    Returns:
        Preview with heuristic suggestions
    """
    session_id = session.get("session_id")
    spans = session.get("spans", [])

    heuristic_suggestions = _generate_heuristic_suggestions(spans, session_id)

    return {
        "session_id": session_id,
        "span_count": len(spans),
        "heuristic_suggestions": [s.to_dict() for s in heuristic_suggestions],
        "suggestion_count": len(heuristic_suggestions),
        "llm_required": True,  # LLM would add more suggestions
    }
