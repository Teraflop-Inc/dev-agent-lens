"""
Tests for the prompts module (Story 4.6).

Test Cases:
1. Default prompts
2. Custom prompt loading
3. Template rendering
4. Validation
5. Prompt types
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from dev_agent_lens.llm.prompts import (
    PromptConfig,
    PromptTemplate,
    PromptType,
    PromptValidationError,
    get_default_prompt,
    get_prompt_info,
    list_available_prompts,
    load_prompt,
    render_prompt,
    save_prompt,
    validate_prompt,
)


class TestDefaultPrompts:
    """Test Case 1: Default prompts."""

    def test_has_summarize_prompt(self):
        """Has default summarize prompt."""
        template = get_default_prompt(PromptType.SUMMARIZE)

        assert template.prompt_type == PromptType.SUMMARIZE
        assert len(template.content) > 100
        assert "{session_id}" in template.content

    def test_has_cluster_prompt(self):
        """Has default cluster prompt."""
        template = get_default_prompt(PromptType.CLUSTER)

        assert template.prompt_type == PromptType.CLUSTER
        assert "{session_count}" in template.content

    def test_has_suggest_prompt(self):
        """Has default suggest prompt."""
        template = get_default_prompt(PromptType.SUGGEST)

        assert template.prompt_type == PromptType.SUGGEST
        assert "{failures}" in template.content

    def test_accepts_string_type(self):
        """Accepts string prompt type."""
        template = get_default_prompt("summarize")

        assert template.prompt_type == PromptType.SUMMARIZE


class TestCustomPromptLoading:
    """Test Case 2: Custom prompt loading."""

    def test_loads_from_file(self):
        """Loads prompt from file."""
        with TemporaryDirectory() as tmpdir:
            prompt_file = Path(tmpdir) / "custom.txt"
            prompt_file.write_text("Custom prompt: {var}")

            template = load_prompt(
                PromptType.SUMMARIZE,
                prompt_file=prompt_file,
            )

            assert "Custom prompt" in template.content

    def test_override_takes_precedence(self):
        """Inline override takes precedence over file."""
        template = load_prompt(
            PromptType.SUMMARIZE,
            prompt_override="Inline prompt {x}",
        )

        assert template.content == "Inline prompt {x}"

    def test_falls_back_to_default(self):
        """Falls back to default when no custom available."""
        template = load_prompt(PromptType.SUMMARIZE)

        # Should get default prompt
        assert "{session_id}" in template.content

    def test_raises_for_missing_file(self):
        """Raises error for missing prompt file."""
        with pytest.raises(PromptValidationError):
            load_prompt(
                PromptType.SUMMARIZE,
                prompt_file="/nonexistent/path.txt",
            )

    def test_loads_from_config_dir(self):
        """Loads from config directory."""
        with TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "summarize.txt").write_text("Config dir prompt")

            config = PromptConfig(prompt_dir=config_dir)
            template = load_prompt(PromptType.SUMMARIZE, config=config)

            assert "Config dir prompt" in template.content


class TestTemplateRendering:
    """Test Case 3: Template rendering."""

    def test_renders_variables(self):
        """Renders template with variables."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Session {session_id} has {span_count} spans.",
        )

        result = render_prompt(template, {
            "session_id": "abc123",
            "span_count": 42,
        })

        assert "abc123" in result
        assert "42" in result

    def test_handles_missing_variables(self):
        """Handles missing variables gracefully."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Session {session_id} in {unknown_var}",
        )

        result = render_prompt(template, {"session_id": "test"})

        assert "test" in result
        assert "{unknown_var}" in result  # Left as-is

    def test_strict_mode_raises_for_missing(self):
        """Strict mode raises error for missing variables."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Session {session_id}",
            required_vars=["session_id"],
        )

        with pytest.raises(PromptValidationError):
            render_prompt(template, {}, strict=True)

    def test_converts_values_to_strings(self):
        """Converts non-string values."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Count: {count}, List: {items}",
        )

        result = render_prompt(template, {
            "count": 100,
            "items": ["a", "b"],
        })

        assert "100" in result
        assert "['a', 'b']" in result


class TestValidation:
    """Test Case 4: Validation."""

    def test_warns_on_undefined_placeholders(self):
        """Warns when placeholders are undefined."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Hello {name} at {location}",
        )

        warnings = validate_prompt(template, {"name": "test"})

        assert any("location" in w for w in warnings)

    def test_warns_on_missing_required(self):
        """Warns on missing required variables."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Hello {name}",
            required_vars=["name"],
        )

        warnings = validate_prompt(template, {})

        assert any("Required" in w for w in warnings)

    def test_no_warnings_for_complete_template(self):
        """No warnings when all variables provided."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Hello {name}",
        )

        # Include at least the variable used in the template
        warnings = validate_prompt(template, {"name": "world"})

        # Filter out informational warnings
        errors = [w for w in warnings if "Undefined" in w or "Required" in w]
        assert len(errors) == 0


class TestPromptTypes:
    """Test Case 5: Prompt types."""

    def test_enum_values(self):
        """Enum has expected values."""
        assert PromptType.SUMMARIZE.value == "summarize"
        assert PromptType.CLUSTER.value == "cluster"
        assert PromptType.SUGGEST.value == "suggest"

    def test_enum_from_string(self):
        """Can create from string."""
        assert PromptType("summarize") == PromptType.SUMMARIZE


class TestPromptTemplate:
    """Tests for PromptTemplate dataclass."""

    def test_extracts_variables_from_content(self):
        """Extracts variable names from content."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Hello {name}, you have {count} items",
        )

        assert "name" in template.optional_vars
        assert "count" in template.optional_vars

    def test_uses_provided_vars(self):
        """Uses explicitly provided variable lists."""
        template = PromptTemplate(
            prompt_type=PromptType.SUMMARIZE,
            content="Hello {name}",
            required_vars=["name"],
            optional_vars=["extra"],
        )

        assert template.required_vars == ["name"]
        assert template.optional_vars == ["extra"]


class TestSavePrompt:
    """Tests for save_prompt function."""

    def test_saves_to_path(self):
        """Saves prompt to specified path."""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            template = PromptTemplate(
                prompt_type=PromptType.SUMMARIZE,
                content="Test content",
            )

            result_path = save_prompt(template, path)

            assert result_path == path
            assert path.read_text() == "Test content"


class TestListAvailablePrompts:
    """Tests for list_available_prompts function."""

    def test_lists_builtin_prompts(self):
        """Lists built-in prompt types."""
        result = list_available_prompts()

        assert "builtin" in result
        assert "summarize" in result["builtin"]
        assert "cluster" in result["builtin"]
        assert "suggest" in result["builtin"]

    def test_lists_custom_prompts(self):
        """Lists custom prompts from directory."""
        with TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "my_prompt.txt").write_text("Custom")

            config = PromptConfig(prompt_dir=config_dir)
            result = list_available_prompts(config)

            assert "custom" in result
            assert "my_prompt" in result["custom"]


class TestGetPromptInfo:
    """Tests for get_prompt_info function."""

    def test_returns_info_dict(self):
        """Returns prompt info dictionary."""
        info = get_prompt_info(PromptType.SUMMARIZE)

        assert "type" in info
        assert "placeholders" in info
        assert "default_length" in info
        assert info["type"] == "summarize"

    def test_includes_placeholders(self):
        """Includes list of placeholders."""
        info = get_prompt_info(PromptType.SUMMARIZE)

        assert isinstance(info["placeholders"], list)
        assert "session_id" in info["placeholders"]


class TestPromptConfig:
    """Tests for PromptConfig dataclass."""

    def test_default_values(self):
        """Has expected default values."""
        config = PromptConfig()

        assert config.prompt_dir is None
        assert config.use_defaults is True

    def test_custom_values(self):
        """Accepts custom values."""
        config = PromptConfig(
            prompt_dir=Path("/custom"),
            use_defaults=False,
        )

        assert config.prompt_dir == Path("/custom")
        assert config.use_defaults is False
