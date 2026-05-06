"""
Tests for the model router module (Story 4.2).

Test Cases:
1. Provider detection
2. Model selection
3. Configuration
4. Error handling
5. Availability checks
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from dev_agent_lens.llm.router import (
    AnalysisType,
    LLMConfig,
    LLMProvider,
    LLMResponse,
    NoLLMConfigError,
    check_llm_availability,
    get_api_key,
    get_available_provider,
    get_available_providers,
    get_llm_config,
    is_provider_available,
)


class TestProviderDetection:
    """Test Case 1: Provider detection."""

    def test_detects_openai_from_env(self):
        """Detects OpenAI when OPENAI_API_KEY is set."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            assert is_provider_available(LLMProvider.OPENAI) is True

    def test_detects_anthropic_from_env(self):
        """Detects Anthropic when ANTHROPIC_API_KEY is set."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            assert is_provider_available(LLMProvider.ANTHROPIC) is True

    def test_no_provider_without_keys(self):
        """No providers available without API keys."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                # Clear any existing keys
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    assert is_provider_available(LLMProvider.OPENAI) is False
                    assert is_provider_available(LLMProvider.ANTHROPIC) is False

    def test_get_available_providers_list(self):
        """Returns list of available providers."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            env = {k: v for k, v in os.environ.items()
                   if k != "ANTHROPIC_API_KEY"}
            env["OPENAI_API_KEY"] = "sk-test"
            with patch.dict(os.environ, env, clear=True):
                providers = get_available_providers()
                assert LLMProvider.OPENAI in providers


class TestModelSelection:
    """Test Case 2: Model selection."""

    def test_selects_openai_for_summarize(self):
        """Selects OpenAI model for summarization."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            provider = get_available_provider(AnalysisType.SUMMARIZE)
            assert provider == LLMProvider.OPENAI

    def test_requires_openai_for_clustering(self):
        """Clustering requires OpenAI for embeddings."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            provider = get_available_provider(AnalysisType.CLUSTER)
            assert provider == LLMProvider.OPENAI

    def test_selects_openai_for_suggest(self):
        """Selects OpenAI for suggestions."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            provider = get_available_provider(AnalysisType.SUGGEST)
            assert provider == LLMProvider.OPENAI

    def test_returns_none_without_provider(self):
        """Returns None when no provider available."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    provider = get_available_provider(AnalysisType.SUMMARIZE)
                    assert provider is None


class TestLLMConfig:
    """Test Case 3: Configuration."""

    def test_creates_config_with_defaults(self):
        """Creates config with default model."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = get_llm_config(AnalysisType.SUMMARIZE)

            assert config.provider == LLMProvider.OPENAI
            assert config.model == "gpt-5-nano"
            assert config.max_tokens == 4096

    def test_allows_model_override(self):
        """Allows explicit model override."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = get_llm_config(
                AnalysisType.SUMMARIZE,
                model="gpt-4-turbo",
            )

            assert config.model == "gpt-4-turbo"

    def test_allows_endpoint_override(self):
        """Allows custom endpoint."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = get_llm_config(
                AnalysisType.SUMMARIZE,
                endpoint="https://custom.api.com",
            )

            assert config.endpoint == "https://custom.api.com"

    def test_config_to_dict_excludes_api_key(self):
        """to_dict excludes sensitive API key."""
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-4o-mini",
            api_key="sk-secret",
        )

        result = config.to_dict()

        assert "api_key" not in result
        assert result["model"] == "gpt-4o-mini"

    def test_accepts_string_analysis_type(self):
        """Accepts string analysis type."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            config = get_llm_config("summarize")

            assert config.provider == LLMProvider.OPENAI


class TestErrorHandling:
    """Test Case 4: Error handling."""

    def test_raises_error_without_config(self):
        """Raises NoLLMConfigError when no provider available."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    with pytest.raises(NoLLMConfigError) as exc:
                        get_llm_config(AnalysisType.SUMMARIZE)

                    assert "No LLM provider configured" in str(exc.value)

    def test_raises_error_for_unavailable_provider(self):
        """Raises error when requested provider is unavailable."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    with pytest.raises(NoLLMConfigError) as exc:
                        get_llm_config(
                            AnalysisType.SUMMARIZE,
                            provider=LLMProvider.OPENAI,
                        )

                    assert "not configured" in str(exc.value)


class TestAvailabilityChecks:
    """Test Case 5: Availability checks."""

    def test_check_availability_structure(self):
        """check_llm_availability returns expected structure."""
        result = check_llm_availability()

        assert "openai_available" in result
        assert "anthropic_available" in result
        assert "available_providers" in result
        assert "summarize_available" in result
        assert "cluster_available" in result
        assert "suggest_available" in result

    def test_availability_reflects_env(self):
        """Availability reflects environment configuration."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = check_llm_availability()

            assert result["openai_available"] is True
            assert result["summarize_available"] is True
            assert result["cluster_available"] is True

    def test_availability_without_keys(self):
        """All features unavailable without keys."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    result = check_llm_availability()

                    assert result["summarize_available"] is False
                    assert result["cluster_available"] is False


class TestLLMResponse:
    """Tests for LLMResponse dataclass."""

    def test_response_to_dict(self):
        """Response converts to dictionary."""
        response = LLMResponse(
            content="Test response",
            model="gpt-4o-mini",
            provider=LLMProvider.OPENAI,
            usage={"total_tokens": 100},
        )

        result = response.to_dict()

        assert result["content"] == "Test response"
        assert result["model"] == "gpt-4o-mini"
        assert result["provider"] == "openai"
        assert result["usage"]["total_tokens"] == 100


class TestGetApiKey:
    """Tests for get_api_key function."""

    def test_returns_openai_key(self):
        """Returns OpenAI API key from env."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
            key = get_api_key(LLMProvider.OPENAI)
            assert key == "sk-test-key"

    def test_returns_anthropic_key(self):
        """Returns Anthropic API key from env."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            key = get_api_key(LLMProvider.ANTHROPIC)
            assert key == "sk-ant-test"

    def test_returns_none_for_missing_key(self):
        """Returns None when key not set."""
        # Mock _load_env_file to prevent reading from ~/.dal/.env
        with patch("dev_agent_lens.llm.router._load_env_file"):
            with patch.dict(os.environ, {}, clear=True):
                env = {k: v for k, v in os.environ.items()
                       if k not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]}
                with patch.dict(os.environ, env, clear=True):
                    key = get_api_key(LLMProvider.OPENAI)
                    assert key is None


class TestAnalysisTypeEnum:
    """Tests for AnalysisType enum."""

    def test_enum_values(self):
        """Enum has expected values."""
        assert AnalysisType.SUMMARIZE.value == "summarize"
        assert AnalysisType.CLUSTER.value == "cluster"
        assert AnalysisType.SUGGEST.value == "suggest"
        assert AnalysisType.EMBED.value == "embed"

    def test_enum_from_string(self):
        """Can create enum from string."""
        assert AnalysisType("summarize") == AnalysisType.SUMMARIZE


class TestLLMProviderEnum:
    """Tests for LLMProvider enum."""

    def test_enum_values(self):
        """Enum has expected values."""
        assert LLMProvider.OPENAI.value == "openai"
        assert LLMProvider.ANTHROPIC.value == "anthropic"
