"""
Prompt Configuration Module (Story 4.6)

Provides customizable prompts for LLM analysis commands.
Supports template loading, validation, and rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PromptType(str, Enum):
    """Types of prompts supported."""

    SUMMARIZE = "summarize"
    CLUSTER = "cluster"
    SUGGEST = "suggest"


@dataclass
class PromptTemplate:
    """A prompt template with placeholders.

    Attributes:
        prompt_type: Type of prompt
        content: Template content with {placeholders}
        required_vars: Variables that must be provided
        optional_vars: Variables that can be provided
    """

    prompt_type: PromptType
    content: str
    required_vars: list[str] = field(default_factory=list)
    optional_vars: list[str] = field(default_factory=list)

    def __post_init__(self):
        """Extract variables from content if not provided."""
        if not self.required_vars and not self.optional_vars:
            # Extract all {var} placeholders
            vars_found = re.findall(r"\{(\w+)\}", self.content)
            self.optional_vars = list(set(vars_found))


@dataclass
class PromptConfig:
    """Configuration for prompt loading.

    Attributes:
        prompt_dir: Directory to load custom prompts from
        use_defaults: Whether to fall back to defaults
    """

    prompt_dir: Path | None = None
    use_defaults: bool = True


# Default prompts for each analysis type
DEFAULT_PROMPTS = {
    PromptType.SUMMARIZE: """Analyze the following trace session and provide a concise summary.

Session ID: {session_id}
Total Spans: {span_count}
Duration: {duration}

Session Data:
{session_data}

Please provide:
1. A brief summary (2-3 sentences) of what this session accomplished
2. Key operations performed (tools used, files accessed)
3. Any errors or failures encountered
4. Overall success assessment
5. Notable patterns or observations

Format your response as a structured summary.""",
    PromptType.CLUSTER: """Analyze the following sessions and identify common patterns for clustering.

Number of Sessions: {session_count}

Session Summaries:
{session_summaries}

Please:
1. Identify distinct categories/clusters of sessions based on their behavior
2. For each cluster, provide:
   - A descriptive label
   - Common characteristics
   - Example session IDs
3. Note any outlier sessions that don't fit well into clusters

Focus on behavioral patterns like:
- Types of tasks (debugging, feature development, exploration)
- Tool usage patterns
- Success/failure patterns
- Duration and complexity patterns""",
    PromptType.SUGGEST: """Analyze the following trace session and provide improvement suggestions.

Session ID: {session_id}
Duration: {duration}
Tool Calls: {tool_count}
Failures: {failure_count}

Session Data:
{session_data}

Failure Details:
{failures}

Provide actionable suggestions. For each issue found, output in this exact format:

1. [Issue Title]
   Category: [error|efficiency|churn|best_practice|performance]
   Severity: [high|medium|low]
   Description: [What the issue is]
   Recommendation: [What to do about it]
   Impact: [Expected benefit]

2. [Next Issue Title]
   ...

Focus on:
- Error handling and failures
- Redundant operations (same file read/edited multiple times)
- Code churn (files created then immediately edited)
- Inefficient tool usage patterns
- Anti-patterns

If the session looks good with no issues, respond with: "No issues found - session looks well-optimized."

List 3-7 suggestions maximum, prioritized by severity.""",
}


class PromptValidationError(Exception):
    """Raised when prompt validation fails."""

    pass


def _get_default_prompt_dir() -> Path:
    """Get the default prompt directory (~/.dal/prompts)."""
    return Path.home() / ".dal" / "prompts"


def get_default_prompt(prompt_type: PromptType | str) -> PromptTemplate:
    """Get the default prompt for a type.

    Args:
        prompt_type: Type of prompt to get

    Returns:
        Default PromptTemplate
    """
    if isinstance(prompt_type, str):
        prompt_type = PromptType(prompt_type)

    content = DEFAULT_PROMPTS.get(prompt_type, "")

    return PromptTemplate(
        prompt_type=prompt_type,
        content=content,
    )


def load_prompt(
    prompt_type: PromptType | str,
    config: PromptConfig | None = None,
    prompt_file: Path | str | None = None,
    prompt_override: str | None = None,
) -> PromptTemplate:
    """Load a prompt from configuration or defaults.

    Priority order:
    1. prompt_override (inline text)
    2. prompt_file (explicit file path)
    3. config.prompt_dir / {prompt_type}.txt
    4. Default prompt

    Args:
        prompt_type: Type of prompt to load
        config: Prompt configuration
        prompt_file: Explicit file path to load from
        prompt_override: Inline prompt text

    Returns:
        PromptTemplate with loaded content

    Raises:
        PromptValidationError: If prompt file cannot be read
    """
    if isinstance(prompt_type, str):
        prompt_type = PromptType(prompt_type)

    # Priority 1: Inline override
    if prompt_override:
        return PromptTemplate(
            prompt_type=prompt_type,
            content=prompt_override,
        )

    # Priority 2: Explicit file path
    if prompt_file:
        prompt_path = Path(prompt_file)
        if not prompt_path.exists():
            raise PromptValidationError(f"Prompt file not found: {prompt_path}")
        try:
            content = prompt_path.read_text()
            return PromptTemplate(
                prompt_type=prompt_type,
                content=content,
            )
        except Exception as e:
            raise PromptValidationError(f"Error reading prompt file: {e}")

    # Priority 3: Config directory
    if config and config.prompt_dir:
        prompt_path = config.prompt_dir / f"{prompt_type.value}.txt"
        if prompt_path.exists():
            try:
                content = prompt_path.read_text()
                return PromptTemplate(
                    prompt_type=prompt_type,
                    content=content,
                )
            except Exception:
                pass  # Fall through to defaults

    # Priority 4: Default prompt directory
    default_dir = _get_default_prompt_dir()
    prompt_path = default_dir / f"{prompt_type.value}.txt"
    if prompt_path.exists():
        try:
            content = prompt_path.read_text()
            return PromptTemplate(
                prompt_type=prompt_type,
                content=content,
            )
        except Exception:
            pass  # Fall through to built-in defaults

    # Fall back to built-in defaults
    if config is None or config.use_defaults:
        return get_default_prompt(prompt_type)

    raise PromptValidationError(
        f"No prompt found for {prompt_type.value} and defaults disabled"
    )


def validate_prompt(
    template: PromptTemplate,
    available_vars: dict[str, Any],
) -> list[str]:
    """Validate a prompt template against available variables.

    Args:
        template: Prompt template to validate
        available_vars: Variables that will be available for rendering

    Returns:
        List of warning messages (empty if valid)
    """
    warnings = []

    # Check required variables
    for var in template.required_vars:
        if var not in available_vars:
            warnings.append(f"Required variable '{var}' not provided")

    # Check for undefined placeholders
    placeholders = set(re.findall(r"\{(\w+)\}", template.content))
    available_keys = set(available_vars.keys())

    missing = placeholders - available_keys
    if missing:
        warnings.append(f"Undefined placeholders: {', '.join(sorted(missing))}")

    # Check for unused variables (informational)
    unused = available_keys - placeholders
    if unused:
        warnings.append(f"Unused variables (informational): {', '.join(sorted(unused))}")

    return warnings


def render_prompt(
    template: PromptTemplate,
    variables: dict[str, Any],
    strict: bool = False,
) -> str:
    """Render a prompt template with variables.

    Args:
        template: Prompt template to render
        variables: Variables to substitute
        strict: If True, raise error on missing variables

    Returns:
        Rendered prompt string

    Raises:
        PromptValidationError: If strict and variables are missing
    """
    if strict:
        warnings = validate_prompt(template, variables)
        errors = [w for w in warnings if "Required" in w or "Undefined" in w]
        if errors:
            raise PromptValidationError("\n".join(errors))

    # Convert all values to strings
    string_vars = {k: str(v) if v is not None else "" for k, v in variables.items()}

    # Use safe substitution that leaves undefined placeholders
    result = template.content
    for key, value in string_vars.items():
        result = result.replace(f"{{{key}}}", value)

    return result


def save_prompt(
    template: PromptTemplate,
    path: Path | str | None = None,
) -> Path:
    """Save a prompt template to disk.

    Args:
        template: Prompt template to save
        path: Path to save to (uses default dir if None)

    Returns:
        Path where prompt was saved
    """
    if path is None:
        prompt_dir = _get_default_prompt_dir()
        prompt_dir.mkdir(parents=True, exist_ok=True)
        path = prompt_dir / f"{template.prompt_type.value}.txt"
    else:
        path = Path(path)

    path.write_text(template.content)
    return path


def list_available_prompts(
    config: PromptConfig | None = None,
) -> dict[str, list[str]]:
    """List available prompts from all sources.

    Args:
        config: Prompt configuration

    Returns:
        Dictionary mapping source to list of prompt names
    """
    result: dict[str, list[str]] = {
        "builtin": [pt.value for pt in PromptType],
        "custom": [],
    }

    # Check default directory
    default_dir = _get_default_prompt_dir()
    if default_dir.exists():
        for file in default_dir.glob("*.txt"):
            name = file.stem
            if name not in result["custom"]:
                result["custom"].append(name)

    # Check config directory
    if config and config.prompt_dir and config.prompt_dir.exists():
        for file in config.prompt_dir.glob("*.txt"):
            name = file.stem
            if name not in result["custom"]:
                result["custom"].append(name)

    return result


def get_prompt_info(prompt_type: PromptType | str) -> dict[str, Any]:
    """Get information about a prompt type.

    Args:
        prompt_type: Type of prompt

    Returns:
        Dictionary with prompt information
    """
    if isinstance(prompt_type, str):
        prompt_type = PromptType(prompt_type)

    template = get_default_prompt(prompt_type)

    # Extract placeholders
    placeholders = sorted(set(re.findall(r"\{(\w+)\}", template.content)))

    return {
        "type": prompt_type.value,
        "placeholders": placeholders,
        "default_length": len(template.content),
        "has_custom": (_get_default_prompt_dir() / f"{prompt_type.value}.txt").exists(),
    }
