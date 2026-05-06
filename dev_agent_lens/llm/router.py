"""
Model Router Module (Story 4.2)

Routes LLM analysis requests to appropriate models based on task type,
availability, and configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class AnalysisType(str, Enum):
    """Types of analysis supported."""

    SUMMARIZE = "summarize"
    CLUSTER = "cluster"
    SUGGEST = "suggest"
    EMBED = "embed"


class NoLLMConfigError(Exception):
    """Raised when no LLM configuration is available."""

    pass


@dataclass
class LLMConfig:
    """Configuration for LLM routing.

    Attributes:
        provider: LLM provider to use
        model: Model name/identifier
        api_key: API key (loaded from environment if None)
        endpoint: Custom API endpoint
        max_tokens: Maximum tokens for response
        temperature: Temperature for generation
    """

    provider: LLMProvider
    model: str
    api_key: str | None = None
    endpoint: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (excluding api_key)."""
        return {
            "provider": self.provider.value,
            "model": self.model,
            "endpoint": self.endpoint,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


@dataclass
class LLMResponse:
    """Response from an LLM request.

    Attributes:
        content: Response text content
        model: Model used for generation
        provider: Provider used
        usage: Token usage information
        metadata: Additional response metadata
    """

    content: str
    model: str
    provider: LLMProvider
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "content": self.content,
            "model": self.model,
            "provider": self.provider.value,
            "usage": self.usage,
            "metadata": self.metadata,
        }


# Default model configurations per analysis type
DEFAULT_MODELS = {
    AnalysisType.SUMMARIZE: {
        LLMProvider.OPENAI: "gpt-5-nano",
        LLMProvider.ANTHROPIC: "claude-3-haiku-20240307",
    },
    AnalysisType.CLUSTER: {
        LLMProvider.OPENAI: "text-embedding-3-small",
    },
    AnalysisType.SUGGEST: {
        LLMProvider.OPENAI: "gpt-5-nano",
        LLMProvider.ANTHROPIC: "claude-3-haiku-20240307",
    },
    AnalysisType.EMBED: {
        LLMProvider.OPENAI: "text-embedding-3-small",
    },
}

# Provider preference order per analysis type
PROVIDER_PREFERENCE = {
    AnalysisType.SUMMARIZE: [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
    AnalysisType.CLUSTER: [LLMProvider.OPENAI],  # Requires OpenAI embeddings
    AnalysisType.SUGGEST: [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
    AnalysisType.EMBED: [LLMProvider.OPENAI],
}


def _load_env_file() -> None:
    """Load environment variables from ~/.dal/.env if it exists."""
    env_path = Path.home() / ".dal" / ".env"
    if env_path.exists():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        # Strip quotes if present
                        value = value.strip().strip("'\"")
                        if key and value and key not in os.environ:
                            os.environ[key] = value
        except Exception:
            pass  # Silently ignore env file errors


def get_api_key(provider: LLMProvider) -> str | None:
    """Get API key for a provider from environment.

    Checks ~/.dal/.env first, then environment variables.

    Args:
        provider: LLM provider

    Returns:
        API key if found, None otherwise
    """
    _load_env_file()

    if provider == LLMProvider.OPENAI:
        return os.environ.get("OPENAI_API_KEY")
    elif provider == LLMProvider.ANTHROPIC:
        return os.environ.get("ANTHROPIC_API_KEY")
    return None


def is_provider_available(provider: LLMProvider) -> bool:
    """Check if a provider is available (has API key configured).

    Args:
        provider: LLM provider to check

    Returns:
        True if provider is available
    """
    return get_api_key(provider) is not None


def get_available_providers() -> list[LLMProvider]:
    """Get list of available providers.

    Returns:
        List of providers with configured API keys
    """
    return [p for p in LLMProvider if is_provider_available(p)]


def get_available_provider(
    analysis_type: AnalysisType | str,
) -> LLMProvider | None:
    """Get the best available provider for an analysis type.

    Args:
        analysis_type: Type of analysis to perform

    Returns:
        Best available provider, or None if none available
    """
    if isinstance(analysis_type, str):
        analysis_type = AnalysisType(analysis_type)

    preference = PROVIDER_PREFERENCE.get(analysis_type, list(LLMProvider))

    for provider in preference:
        if is_provider_available(provider):
            return provider

    return None


def get_llm_config(
    analysis_type: AnalysisType | str,
    provider: LLMProvider | str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> LLMConfig:
    """Get LLM configuration for an analysis type.

    Args:
        analysis_type: Type of analysis to perform
        provider: Specific provider to use (auto-selects if None)
        model: Specific model to use (uses default if None)
        endpoint: Custom API endpoint
        max_tokens: Maximum tokens for response
        temperature: Temperature for generation

    Returns:
        LLMConfig for the request

    Raises:
        NoLLMConfigError: If no provider is available
    """
    if isinstance(analysis_type, str):
        analysis_type = AnalysisType(analysis_type)

    # Determine provider
    if provider is not None:
        if isinstance(provider, str):
            provider = LLMProvider(provider)
        if not is_provider_available(provider):
            raise NoLLMConfigError(
                f"Provider {provider.value} is not configured. "
                f"Please set {provider.value.upper()}_API_KEY in ~/.dal/.env or environment."
            )
    else:
        provider = get_available_provider(analysis_type)
        if provider is None:
            required_providers = PROVIDER_PREFERENCE.get(analysis_type, list(LLMProvider))
            provider_names = [p.value for p in required_providers]
            raise NoLLMConfigError(
                f"No LLM provider configured for {analysis_type.value}. "
                f"Please set one of: {', '.join(p.upper() + '_API_KEY' for p in provider_names)} "
                f"in ~/.dal/.env or environment."
            )

    # Determine model
    if model is None:
        model_defaults = DEFAULT_MODELS.get(analysis_type, {})
        model = model_defaults.get(provider)
        if model is None:
            # Fall back to a reasonable default
            if provider == LLMProvider.OPENAI:
                model = "gpt-5-nano"
            else:
                model = "claude-3-haiku-20240307"

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=get_api_key(provider),
        endpoint=endpoint,
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def route_request(
    prompt: str,
    config: LLMConfig,
    system_prompt: str | None = None,
) -> LLMResponse:
    """Route a request to the appropriate LLM.

    Args:
        prompt: The prompt/query to send
        config: LLM configuration
        system_prompt: Optional system prompt

    Returns:
        LLMResponse with the result

    Raises:
        NoLLMConfigError: If no API key is available
        Exception: For API errors
    """
    if config.api_key is None:
        raise NoLLMConfigError(
            f"No API key configured for {config.provider.value}. "
            f"Please set {config.provider.value.upper()}_API_KEY."
        )

    if config.provider == LLMProvider.OPENAI:
        return await _call_openai(prompt, config, system_prompt)
    elif config.provider == LLMProvider.ANTHROPIC:
        return await _call_anthropic(prompt, config, system_prompt)
    else:
        raise ValueError(f"Unsupported provider: {config.provider}")


async def _call_openai(
    prompt: str,
    config: LLMConfig,
    system_prompt: str | None = None,
) -> LLMResponse:
    """Call OpenAI API.

    Args:
        prompt: User prompt
        config: LLM configuration
        system_prompt: Optional system prompt

    Returns:
        LLMResponse with result
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise ImportError(
            "openai package is required for OpenAI support. "
            "Install with: pip install openai"
        )

    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.endpoint,
    )

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # Use max_completion_tokens for newer models (gpt-5-nano, o1, etc.)
    # These models also don't support custom temperature
    if config.model.startswith(("gpt-5", "o1", "o3")):
        response = await client.chat.completions.create(
            model=config.model,
            messages=messages,
            max_completion_tokens=config.max_tokens,
        )
    else:
        response = await client.chat.completions.create(
            model=config.model,
            messages=messages,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

    content = response.choices[0].message.content or ""
    usage = {}
    if response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }

    return LLMResponse(
        content=content,
        model=config.model,
        provider=LLMProvider.OPENAI,
        usage=usage,
        metadata={"finish_reason": response.choices[0].finish_reason},
    )


async def _call_anthropic(
    prompt: str,
    config: LLMConfig,
    system_prompt: str | None = None,
) -> LLMResponse:
    """Call Anthropic API.

    Args:
        prompt: User prompt
        config: LLM configuration
        system_prompt: Optional system prompt

    Returns:
        LLMResponse with result
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        raise ImportError(
            "anthropic package is required for Anthropic support. "
            "Install with: pip install anthropic"
        )

    client = AsyncAnthropic(
        api_key=config.api_key,
        base_url=config.endpoint,
    )

    kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    if system_prompt:
        kwargs["system"] = system_prompt

    response = await client.messages.create(**kwargs)

    content = ""
    if response.content:
        content = response.content[0].text

    usage = {}
    if response.usage:
        usage = {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        }

    return LLMResponse(
        content=content,
        model=config.model,
        provider=LLMProvider.ANTHROPIC,
        usage=usage,
        metadata={"stop_reason": response.stop_reason},
    )


async def get_embeddings(
    texts: list[str],
    config: LLMConfig | None = None,
) -> list[list[float]]:
    """Get embeddings for a list of texts.

    Args:
        texts: List of texts to embed
        config: LLM configuration (uses OpenAI embeddings by default)

    Returns:
        List of embedding vectors

    Raises:
        NoLLMConfigError: If OpenAI is not configured
    """
    if config is None:
        config = get_llm_config(AnalysisType.EMBED)

    if config.provider != LLMProvider.OPENAI:
        raise ValueError("Embeddings currently only support OpenAI")

    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise ImportError(
            "openai package is required for embeddings. "
            "Install with: pip install openai"
        )

    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.endpoint,
    )

    response = await client.embeddings.create(
        model=config.model,
        input=texts,
    )

    return [item.embedding for item in response.data]


def check_llm_availability() -> dict[str, Any]:
    """Check LLM availability and configuration status.

    Returns:
        Dictionary with availability information
    """
    available = get_available_providers()

    return {
        "openai_available": LLMProvider.OPENAI in available,
        "anthropic_available": LLMProvider.ANTHROPIC in available,
        "available_providers": [p.value for p in available],
        "summarize_available": get_available_provider(AnalysisType.SUMMARIZE) is not None,
        "cluster_available": get_available_provider(AnalysisType.CLUSTER) is not None,
        "suggest_available": get_available_provider(AnalysisType.SUGGEST) is not None,
    }
