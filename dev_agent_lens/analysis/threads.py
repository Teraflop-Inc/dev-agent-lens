"""
Conversation Thread Classification Module

Classifies spans within a session into conversation threads:
- main_thread: Primary user <-> agent conversation (Sonnet/Opus models)
- ancillary: Status line, topic detection, UI state checks (Haiku models)
- sub_agent: Task tool invocations and their execution spans

This is different from classify.py which categorizes span types (tools, internal, etc.).
This module focuses on which "conversation thread" a span belongs to.

This module is designed for Claude Code traces. Other coding agents
(Cursor, Windsurf, etc.) may require different classification rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ThreadType(Enum):
    """Classification of span thread type."""

    MAIN_THREAD = "main_thread"
    ANCILLARY = "ancillary"
    SUB_AGENT = "sub_agent"
    UNKNOWN = "unknown"


@dataclass
class ThreadClassification:
    """Result of thread classification for a span."""

    thread_type: ThreadType
    reason: str
    confidence: float = 1.0
    is_compaction: bool = False
    compaction_type: str | None = None


@dataclass
class ClassifiedSession:
    """A session with spans classified by thread type."""

    session_id: str
    main_thread: list[dict[str, Any]] = field(default_factory=list)
    ancillary: list[dict[str, Any]] = field(default_factory=list)
    sub_agents: list[dict[str, Any]] = field(default_factory=list)
    # Metadata
    total_spans: int = 0
    classification_stats: dict[str, int] = field(default_factory=dict)
    compaction_count: int = 0
    has_compaction: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "main_thread": self.main_thread,
            "ancillary": self.ancillary,
            "sub_agents": self.sub_agents,
            "metadata": {
                "total_spans": self.total_spans,
                "classification_stats": self.classification_stats,
                "compaction_count": self.compaction_count,
                "has_compaction": self.has_compaction,
            },
        }


def _safe_str(value: Any) -> str:
    """Convert value to string, handling NaN and None gracefully."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value) if value else ""


# =============================================================================
# CLAUDE CODE CLASSIFICATION RULES
# =============================================================================
# These rules are specific to Claude Code traces via LiteLLM proxy.
# Other agents may need different rules added below.
#
# CLASSIFICATION STRATEGY:
# - NAME-FIRST for structural spans (Claude_Code_Internal_*, Claude_Code_Final_*, etc.)
#   These are ALWAYS main_thread regardless of content patterns they may contain.
# - PATTERN-FIRST for other spans (to distinguish ancillary operations)
#
# WHY NAME-FIRST FOR STRUCTURAL SPANS:
# Claude_Code_Internal_Prompt_* spans contain the FULL conversation context,
# including tool results, system reminders, and other ancillary content from
# previous turns. If we classify by content patterns first, these spans would
# be incorrectly marked as ancillary, causing "Main thread turns: 0" in exports.

# Span names that indicate sub-agent activity
SUB_AGENT_SPAN_NAMES = [
    "Claude_Code_Tool_Task",
]

# =============================================================================
# ANCILLARY INPUT PATTERNS (checked first - most reliable)
# =============================================================================
# These patterns identify ancillary requests regardless of model.
# Discovered via parallel Opus sub-agent analysis of 500 unclassified spans.

# Command display check: Input starts with "Command:" + bash output
# Used to determine if output should be shown to user
ANCILLARY_INPUT_COMMAND_PREFIX = "Command:"

# Quota check: Input is exactly "quota"
ANCILLARY_INPUT_QUOTA = "quota"

# Risk detection: Input contains policy spec for bash command evaluation
ANCILLARY_INPUT_POLICY_SPEC = "<policy_spec>"

# File prioritization: Input lists files modified by user
ANCILLARY_INPUT_FILES_MODIFIED = "Files modified by user:"

# Title generation: Haiku generates conversation titles
# Patterns: "write a 5-10 word title", "title for the following conversation"
ANCILLARY_INPUT_TITLE_GENERATION = "write a 5-10 word title"
ANCILLARY_INPUT_TITLE_GENERATION_ALT = "title for the following conversation"

# WebFetch content processing: Haiku processes fetched web page content
ANCILLARY_INPUT_WEBFETCH = "Web page content:"

# Tool result continuation: Haiku orchestrating agentic tool loop
# Input contains tool_use_id and tool_result markers
ANCILLARY_INPUT_TOOL_RESULT = '"tool_result"'
ANCILLARY_INPUT_TOOL_USE_ID = '"tool_use_id"'

# System reminders: Agent initialization/context injection
ANCILLARY_INPUT_SYSTEM_REMINDER = "<system-reminder>"

# =============================================================================
# ANCILLARY OUTPUT PATTERNS (checked second)
# =============================================================================
# These patterns in the output confirm ancillary purpose.

ANCILLARY_OUTPUT_PATTERNS = [
    "<is_displaying_contents>",   # Content display check result
    '"isNewTopic"',               # Topic detection result
    '"is_displaying_contents"',   # Alternative format
]

# Quota check output is exactly "#" (very short)
ANCILLARY_OUTPUT_QUOTA = "#"

# Delegation check: Haiku returns just "{" to signal delegation decision
# This appears as exactly "{" or wrapped in JSON
ANCILLARY_OUTPUT_DELEGATION = "{"
ANCILLARY_OUTPUT_DELEGATION_JSON = '[{"type": "text", "text": "{"}]'

# Security check: Haiku detects command injection attempts
ANCILLARY_OUTPUT_SECURITY_CHECK = "command_injection_detected"

# =============================================================================
# ROUND 2 PATTERNS (discovered via second sub-agent analysis)
# =============================================================================

# Warmup/Cache priming: Haiku receives warmup request for cache initialization
ANCILLARY_INPUT_WARMUP = '"Warmup"'
ANCILLARY_INPUT_CACHE_CONTROL = '"cache_control"'

# Introduction/Greeting: Haiku announces capabilities at session start
ANCILLARY_OUTPUT_INTRO_READY = "I'm ready to help! I'm Claude Code"
ANCILLARY_OUTPUT_INTRO_HELLO = "Hello! I'm Claude Code"

# Conversation continuation: Haiku coordination between tool calls
# (Empty input with transitional phrases like "Let me check...", "Perfect!")
ANCILLARY_CONTINUATION_PHRASES = [
    "Let me check",
    "Let me examine",
    "Let me search",
    "Let me look",
    "Now let me",
    "Perfect! Now let me",
    "Perfect! Let me",
    "Excellent! Let me",
    "Great! Let me",
    "Great! I found",
]

# =============================================================================
# COMPACTION PATTERNS (conversation summarization when context runs out)
# =============================================================================
# These are NOT ancillary - they're main thread operations for context management
#
# DETECTION LAYERS:
# Layer 1: Exact text match (fast, high precision ~99%)
# Layer 2: Structural detection (fallback for changed prompts, ~85% precision)
# Layer 3: Semantic/LLM detection (future work - for ambiguous cases)

# Layer 1: Exact text markers (current Claude Code prompts as of Jan 2025)
# Compaction task: Claude summarizes conversation before context exhaustion
COMPACTION_TASK_MARKER = "Your task is to create a detailed summary"

# Post-compaction continuation: New conversation with embedded summary
COMPACTION_CONTINUATION_MARKER = "This session is being continued from a previous conversation"
COMPACTION_SUMMARY_MARKER = "The conversation is summarized below:"

# Layer 2: Structural detection signals
# These catch compaction even if exact text changes

# Minimum input length that suggests a summary is embedded (characters)
STRUCTURAL_MIN_INPUT_LENGTH = 5000

# Markdown headers commonly used in compaction summaries
STRUCTURAL_SUMMARY_HEADERS = [
    "## Summary",
    "## Context",
    "## Background",
    "## Previous Conversation",
    "## Conversation Summary",
    "## Session Summary",
    "# Summary",
    "# Context",
]

# Keywords that combined with long input suggest compaction
STRUCTURAL_SUMMARY_KEYWORDS = [
    "summary",
    "summarized",
    "summarize",
    "previous conversation",
    "continued from",
    "context window",
    "earlier discussion",
    "prior session",
]

# Span name patterns that are typical for compaction
STRUCTURAL_COMPACTION_SPAN_PATTERNS = [
    "Claude_Code_Internal_Prompt",
    "litellm_request",
    "raw_gen_ai_request",
]


def _has_ancillary_input_pattern(input_value: str) -> tuple[bool, str]:
    """
    Check if input matches known ancillary patterns.

    Returns:
        Tuple of (is_ancillary, pattern_name)
    """
    if not input_value:
        return False, ""

    # Extract text from JSON array format if present
    text = input_value
    if input_value.startswith("[{") or input_value.startswith("[{'"):
        # Try to extract the text field from JSON
        import json
        import ast
        try:
            parsed = json.loads(input_value)
            text = parsed[0].get("text", "") if parsed else input_value
        except (json.JSONDecodeError, KeyError, IndexError):
            try:
                parsed = ast.literal_eval(input_value)
                text = parsed[0].get("text", "") if parsed else input_value
            except (ValueError, KeyError, IndexError):
                pass

    # Check for exact quota input
    if text.strip() == ANCILLARY_INPUT_QUOTA:
        return True, "quota_check"

    # Check for command display check (also handles JSON-wrapped variant)
    if text.startswith(ANCILLARY_INPUT_COMMAND_PREFIX):
        return True, "command_display_check"
    # JSON variant: '"text": "Command:' in raw input
    if '"text": "Command:' in input_value and "\\nOutput:" in input_value:
        return True, "command_display_check"

    # Check for risk detection (policy spec)
    if ANCILLARY_INPUT_POLICY_SPEC in text:
        return True, "risk_detection"

    # Check for file prioritization
    if text.startswith(ANCILLARY_INPUT_FILES_MODIFIED):
        return True, "files_modified_prioritization"

    # Check for title generation (discovered via Opus analysis)
    if ANCILLARY_INPUT_TITLE_GENERATION.lower() in text.lower():
        return True, "title_generation"
    if ANCILLARY_INPUT_TITLE_GENERATION_ALT.lower() in text.lower():
        return True, "title_generation"

    # Check for WebFetch content processing
    if ANCILLARY_INPUT_WEBFETCH in text:
        return True, "webfetch_processing"

    # Check for tool result continuation (agentic orchestration)
    # This is the biggest category (~55% of previously unclassified Haiku)
    if ANCILLARY_INPUT_TOOL_RESULT in input_value and ANCILLARY_INPUT_TOOL_USE_ID in input_value:
        return True, "tool_result_continuation"

    # Check for system reminders (agent initialization)
    if ANCILLARY_INPUT_SYSTEM_REMINDER in input_value:
        return True, "system_reminder"

    # Check for warmup/cache priming (Round 2 discovery)
    if ANCILLARY_INPUT_WARMUP in input_value and ANCILLARY_INPUT_CACHE_CONTROL in input_value:
        return True, "warmup_cache_priming"

    return False, ""


def _has_ancillary_output_pattern(output_value: str) -> tuple[bool, str]:
    """
    Check if output matches known ancillary patterns.

    Returns:
        Tuple of (is_ancillary, pattern_name)
    """
    if not output_value:
        return False, ""

    # Check for content display check output
    if "<is_displaying_contents>" in output_value:
        return True, "content_display_check"

    # Check for topic detection output
    if '"isNewTopic"' in output_value:
        return True, "topic_detection"
    if '"is_displaying_contents"' in output_value:
        return True, "content_display_check"

    # Check for quota output (exactly "#")
    if output_value.strip() == ANCILLARY_OUTPUT_QUOTA:
        return True, "quota_check"

    # Check for delegation decision output (exactly "{" or JSON-wrapped)
    # Discovered via Opus analysis - Haiku signals delegation with just "{"
    if output_value.strip() == ANCILLARY_OUTPUT_DELEGATION:
        return True, "delegation_check"
    if output_value.strip() == ANCILLARY_OUTPUT_DELEGATION_JSON:
        return True, "delegation_check"

    # Check for security check output (command injection detection)
    if ANCILLARY_OUTPUT_SECURITY_CHECK in output_value:
        return True, "security_check"

    # Check for introduction/greeting (Round 2 discovery)
    if output_value.startswith(ANCILLARY_OUTPUT_INTRO_READY):
        return True, "introduction_greeting"
    if output_value.startswith(ANCILLARY_OUTPUT_INTRO_HELLO):
        return True, "introduction_greeting"

    return False, ""


def _is_sub_agent_span(name: str) -> bool:
    """Check if span is a sub-agent (Task tool) invocation."""
    return name in SUB_AGENT_SPAN_NAMES


# =============================================================================
# LAYERED COMPACTION DETECTION
# =============================================================================


def _detect_compaction_layer1(input_value: str) -> tuple[bool, str, float]:
    """
    Layer 1: Exact text marker detection.

    Fastest and most precise detection method. Matches known Claude Code
    compaction prompts exactly.

    Args:
        input_value: The span's input value.

    Returns:
        Tuple of (is_compaction, compaction_type, confidence)
    """
    if not input_value:
        return False, "", 0.0

    if COMPACTION_TASK_MARKER in input_value:
        return True, "compaction_task", 0.99

    if COMPACTION_CONTINUATION_MARKER in input_value:
        return True, "post_compaction", 0.99

    return False, "", 0.0


def _detect_compaction_layer2(
    input_value: str,
    span_name: str = "",
) -> tuple[bool, str, float]:
    """
    Layer 2: Structural detection for compaction.

    Fallback detection when exact markers aren't present. Uses structural
    signals like input length, markdown headers, and summary keywords.
    This helps catch compaction even if Claude Code changes their prompts.

    Detection heuristics:
    1. Long input (>5000 chars) + summary header = likely post-compaction
    2. Long input + multiple summary keywords = possible compaction
    3. Span name pattern + summary keywords = medium confidence

    Args:
        input_value: The span's input value.
        span_name: The span's name (for pattern matching).

    Returns:
        Tuple of (is_compaction, compaction_type, confidence)
    """
    if not input_value:
        return False, "", 0.0

    input_lower = input_value.lower()
    input_length = len(input_value)
    confidence = 0.0
    signals = []

    # Check for summary headers (strong signal)
    has_summary_header = any(
        header.lower() in input_lower for header in STRUCTURAL_SUMMARY_HEADERS
    )
    if has_summary_header:
        confidence += 0.35
        signals.append("summary_header")

    # Check for summary keywords
    keyword_count = sum(
        1 for kw in STRUCTURAL_SUMMARY_KEYWORDS if kw.lower() in input_lower
    )
    if keyword_count >= 2:
        confidence += 0.25
        signals.append(f"keywords({keyword_count})")
    elif keyword_count == 1:
        confidence += 0.10
        signals.append(f"keywords({keyword_count})")

    # Check input length (long inputs with summary signals suggest compaction)
    if input_length >= STRUCTURAL_MIN_INPUT_LENGTH:
        confidence += 0.20
        signals.append("long_input")
    elif input_length >= STRUCTURAL_MIN_INPUT_LENGTH // 2:
        confidence += 0.10
        signals.append("medium_input")

    # Check span name pattern (typical compaction spans)
    is_typical_span = any(
        pattern in span_name for pattern in STRUCTURAL_COMPACTION_SPAN_PATTERNS
    )
    if is_typical_span:
        confidence += 0.10
        signals.append("typical_span")

    # Determine compaction type based on signals
    # Threshold of 0.55 requires at least 2 strong signals or 3+ weak signals
    # to avoid false positives on normal conversations with summary headers
    if confidence >= 0.55:
        # Determine if it's a task (creating summary) or continuation (using summary)
        # Task markers typically ask to "create" or "generate" summary
        # Continuation markers typically mention "continued" or "previous"
        is_task_like = any(
            word in input_lower
            for word in ["create a", "generate a", "write a", "your task is"]
        )
        is_continuation_like = any(
            word in input_lower
            for word in ["continued from", "previous conversation", "summarized below"]
        )

        if is_task_like and not is_continuation_like:
            compaction_type = "compaction_task_structural"
        elif is_continuation_like:
            compaction_type = "post_compaction_structural"
        else:
            compaction_type = "compaction_structural"

        return True, compaction_type, min(confidence, 0.85)

    return False, "", confidence


# =============================================================================
# Layer 3: Semantic/LLM Detection (FUTURE WORK)
# =============================================================================
# TODO: Implement semantic detection for ambiguous cases where:
# - Layer 1 exact markers don't match (prompt text changed)
# - Layer 2 structural signals are inconclusive (0.3 < confidence < 0.5)
#
# Approach:
# - Use a small/fast model (Haiku) to classify the span
# - Cache results to avoid repeated LLM calls
# - Only invoke when both Layer 1 and Layer 2 are uncertain
#
# async def _detect_compaction_layer3(
#     input_value: str,
#     span_name: str = "",
#     model: str = "claude-3-haiku",
# ) -> tuple[bool, str, float]:
#     """
#     Layer 3: Semantic/LLM detection for ambiguous cases.
#
#     Uses an LLM to classify whether a span represents compaction.
#     Only called when Layer 1 and Layer 2 are uncertain.
#
#     Args:
#         input_value: The span's input value.
#         span_name: The span's name.
#         model: The model to use for classification.
#
#     Returns:
#         Tuple of (is_compaction, compaction_type, confidence)
#     """
#     # Prompt the LLM to classify:
#     # prompt = f"""Analyze this span input and determine if it represents
#     # a conversation compaction/summarization event.
#     #
#     # Span name: {span_name}
#     # Input (first 2000 chars): {input_value[:2000]}
#     #
#     # Return JSON: {{"is_compaction": bool, "type": "task"|"continuation"|"none"}}
#     # """
#     # response = await llm_call(model, prompt)
#     # return parse_response(response)
#     pass


def detect_compaction_layered(
    input_value: str,
    span_name: str = "",
) -> tuple[bool, str, float, str]:
    """
    Layered compaction detection with fallbacks.

    Combines multiple detection strategies for robust compaction identification:
    - Layer 1: Exact text match (fast, ~99% precision)
    - Layer 2: Structural detection (medium, ~85% precision)
    - Layer 3: Semantic/LLM (future work)

    Args:
        input_value: The span's input value.
        span_name: The span's name for pattern matching.

    Returns:
        Tuple of (is_compaction, compaction_type, confidence, detection_layer)
        detection_layer is one of: "layer1_exact", "layer2_structural", "none"
    """
    # Layer 1: Exact text match (highest confidence)
    is_compaction, compaction_type, confidence = _detect_compaction_layer1(input_value)
    if is_compaction:
        return True, compaction_type, confidence, "layer1_exact"

    # Layer 2: Structural detection (fallback)
    is_compaction, compaction_type, confidence = _detect_compaction_layer2(
        input_value, span_name
    )
    if is_compaction:
        return True, compaction_type, confidence, "layer2_structural"

    # Layer 3: Semantic detection (future work)
    # TODO: Implement when Layer 1 and Layer 2 are uncertain
    # if 0.3 < confidence < 0.5:
    #     is_compaction, compaction_type, confidence = await _detect_compaction_layer3(
    #         input_value, span_name
    #     )
    #     if is_compaction:
    #         return True, compaction_type, confidence, "layer3_semantic"

    return False, "", confidence, "none"


def is_compaction_span(input_value: str, span_name: str = "") -> tuple[bool, str]:
    """
    Check if a span is related to conversation compaction.

    Uses layered detection: exact text match first, then structural fallback.
    These are MAIN_THREAD spans, not ancillary - they're the actual
    conversation that happens to have a compaction summary embedded.

    Args:
        input_value: The span's input value.
        span_name: Optional span name for structural detection.

    Returns:
        Tuple of (is_compaction, compaction_type)
        compaction_type is one of: "compaction_task", "post_compaction",
        "compaction_task_structural", "post_compaction_structural",
        "compaction_structural", ""
    """
    is_compaction, compaction_type, _confidence, _layer = detect_compaction_layered(
        input_value, span_name
    )
    return is_compaction, compaction_type


def classify_span_thread(span: dict[str, Any]) -> ThreadClassification:
    """
    Classify a single span into a conversation thread type.

    Classification Rules (Claude Code specific) - NAME-FIRST for main spans:
    1. Sub-agent: Claude_Code_Tool_Task spans
    2. Main thread STRUCTURAL: Claude_Code_Internal_*, Claude_Code_Final_*, tool spans, LLM requests
       These are ALWAYS main thread regardless of content patterns
    3. Ancillary: Check INPUT patterns (for spans not matched by name)
    4. Ancillary: Check OUTPUT patterns (for spans not matched by name)
    5. Compaction: Detect compaction patterns (orthogonal to thread_type)

    IMPORTANT: Claude_Code_Internal_Prompt_* spans contain the full conversation
    context including tool results and system reminders from previous turns.
    These should be classified as main_thread based on their structural role,
    not by the ancillary content patterns they may contain.

    Compaction detection is independent of thread classification - a span can
    be both main_thread AND compaction.

    Args:
        span: The span dictionary.

    Returns:
        ThreadClassification with thread_type, reason, confidence, and compaction flags.
    """
    name = _safe_str(span.get("name"))
    model = _safe_str(span.get("llm_model_name"))
    input_val = _safe_str(span.get("input_value"))
    output = _safe_str(span.get("output_value"))

    # Check for compaction patterns (independent of thread type)
    # Pass span name for structural detection fallback
    is_compaction, compaction_type = is_compaction_span(input_val, name)

    # Rule 1: Sub-agent Task tool invocations
    if _is_sub_agent_span(name):
        return ThreadClassification(
            thread_type=ThreadType.SUB_AGENT,
            reason=f"span_name={name}",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    # Rule 2: Main thread STRUCTURAL classification (by span name patterns)
    # These spans are ALWAYS main thread regardless of input/output patterns
    # because they represent the primary conversation structure in Claude Code

    if name.startswith("Claude_Code_Internal_"):
        return ThreadClassification(
            thread_type=ThreadType.MAIN_THREAD,
            reason="internal_span",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    if name.startswith("Claude_Code_Final_"):
        return ThreadClassification(
            thread_type=ThreadType.MAIN_THREAD,
            reason="final_output_span",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    if name.startswith("Claude_Code_Tool_"):
        return ThreadClassification(
            thread_type=ThreadType.MAIN_THREAD,
            reason="tool_span",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    if name in ("litellm_request", "raw_gen_ai_request"):
        return ThreadClassification(
            thread_type=ThreadType.MAIN_THREAD,
            reason=f"llm_request, model={model}" if model else "llm_request",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    # LiteLLM proxy internal spans (infrastructure for main conversation)
    litellm_proxy_spans = (
        "proxy_pre_call",
        "router",
        "Received Proxy Server Request",
    )
    if name in litellm_proxy_spans:
        return ThreadClassification(
            thread_type=ThreadType.MAIN_THREAD,
            reason="litellm_proxy_span",
            confidence=0.9,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    # Rule 3: Ancillary INPUT patterns (for non-structural spans)
    # Only check these AFTER structural classification to avoid false positives
    is_ancillary_input, input_pattern = _has_ancillary_input_pattern(input_val)
    if is_ancillary_input:
        return ThreadClassification(
            thread_type=ThreadType.ANCILLARY,
            reason=f"input_pattern={input_pattern}",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    # Rule 4: Ancillary OUTPUT patterns (confirms ancillary purpose)
    is_ancillary_output, output_pattern = _has_ancillary_output_pattern(output)
    if is_ancillary_output:
        return ThreadClassification(
            thread_type=ThreadType.ANCILLARY,
            reason=f"output_pattern={output_pattern}",
            confidence=1.0,
            is_compaction=is_compaction,
            compaction_type=compaction_type,
        )

    # Rule 5: Conversation continuation (Round 2 discovery)
    # Empty input with transitional phrases like "Let me check...", "Perfect!"
    # These are Haiku coordination messages between tool calls
    if not input_val.strip() and output:
        for phrase in ANCILLARY_CONTINUATION_PHRASES:
            if output.startswith(phrase):
                return ThreadClassification(
                    thread_type=ThreadType.ANCILLARY,
                    reason="conversation_continuation",
                    confidence=0.95,
                    is_compaction=is_compaction,
                    compaction_type=compaction_type,
                )

    # NOTE: Session title storage pattern (empty input + short text output) was
    # identified but is too aggressive - it matches legitimate short responses.
    # The security_check output pattern catches the main remaining case.
    # Session titles can be added later with more specific heuristics.

    # Unknown span type - default to main thread
    return ThreadClassification(
        thread_type=ThreadType.UNKNOWN,
        reason=f"unclassified, name={name}",
        confidence=0.5,
        is_compaction=is_compaction,
        compaction_type=compaction_type,
    )


def classify_session_threads(session: dict[str, Any]) -> ClassifiedSession:
    """
    Classify all spans in a session by conversation thread.

    Args:
        session: Session dictionary with "session_id" and "spans" keys.

    Returns:
        ClassifiedSession with spans organized by thread type.
    """
    session_id = session.get("session_id", "unknown")
    spans = session.get("spans", [])

    result = ClassifiedSession(session_id=session_id)
    result.total_spans = len(spans)

    stats: dict[str, int] = {
        "main_thread": 0,
        "ancillary": 0,
        "sub_agent": 0,
        "unknown": 0,
    }

    for span in spans:
        classification = classify_span_thread(span)

        # Add classification metadata to span
        classified_span = {
            **span,
            "_thread_type": classification.thread_type.value,
            "_classification_reason": classification.reason,
            "_classification_confidence": classification.confidence,
            "_is_compaction": classification.is_compaction,
            "_compaction_type": classification.compaction_type,
        }

        # Track compaction stats
        if classification.is_compaction:
            result.compaction_count += 1
            result.has_compaction = True

        if classification.thread_type == ThreadType.MAIN_THREAD:
            result.main_thread.append(classified_span)
            stats["main_thread"] += 1
        elif classification.thread_type == ThreadType.ANCILLARY:
            result.ancillary.append(classified_span)
            stats["ancillary"] += 1
        elif classification.thread_type == ThreadType.SUB_AGENT:
            result.sub_agents.append(classified_span)
            stats["sub_agent"] += 1
        else:
            # Unknown goes to main thread as fallback
            result.main_thread.append(classified_span)
            stats["unknown"] += 1

    result.classification_stats = stats
    return result


def classify_sessions_threads(
    sessions: list[dict[str, Any]]
) -> list[ClassifiedSession]:
    """
    Classify multiple sessions by conversation thread.

    Args:
        sessions: List of session dictionaries.

    Returns:
        List of ClassifiedSession objects.
    """
    return [classify_session_threads(s) for s in sessions]


def get_thread_summary(session: dict[str, Any]) -> dict[str, int]:
    """
    Get a summary count of spans by thread type for a session.

    Args:
        session: Session dictionary with spans.

    Returns:
        Dictionary mapping thread type names to counts.
    """
    classified = classify_session_threads(session)
    return classified.classification_stats
