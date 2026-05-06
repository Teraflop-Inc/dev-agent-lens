"""
LLM Analysis Module

Provides LLM-powered analysis capabilities for trace data, including:
- Batch formatting for LLM input
- Model routing and API management
- Session summarization
- Session clustering
- Improvement suggestions
- Custom prompt configuration
"""

from dev_agent_lens.llm.batch import (
    Batch,
    BatchConfig,
    PARQUET_BATCH_FIELDS,
    create_batches,
    create_batches_from_parquet,
    format_batch,
    format_batch_from_parquet,
    format_session_batch,
    format_spans_for_llm,
    get_batch_summary,
)
from dev_agent_lens.llm.cluster import (
    Cluster,
    ClusterResult,
    cluster_sessions,
    cluster_sessions_sync,
    get_cluster_preview,
)
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
    get_embeddings,
    get_llm_config,
    is_provider_available,
    route_request,
)
from dev_agent_lens.llm.suggest import (
    Suggestion,
    SuggestionCategory,
    SuggestionResult,
    SuggestionSeverity,
    get_suggestion_preview,
    suggest_improvements,
    suggest_improvements_sync,
)
from dev_agent_lens.llm.summarize import (
    SessionSummary,
    get_summary_preview,
    summarize_session,
    summarize_session_sync,
    summarize_sessions_batch,
)

__all__ = [
    # Batch formatting
    "Batch",
    "BatchConfig",
    "PARQUET_BATCH_FIELDS",
    "create_batches",
    "create_batches_from_parquet",
    "format_batch",
    "format_batch_from_parquet",
    "format_session_batch",
    "format_spans_for_llm",
    "get_batch_summary",
    # Clustering
    "Cluster",
    "ClusterResult",
    "cluster_sessions",
    "cluster_sessions_sync",
    "get_cluster_preview",
    # Prompts
    "PromptConfig",
    "PromptTemplate",
    "PromptType",
    "PromptValidationError",
    "get_default_prompt",
    "get_prompt_info",
    "list_available_prompts",
    "load_prompt",
    "render_prompt",
    "save_prompt",
    "validate_prompt",
    # Router
    "AnalysisType",
    "LLMConfig",
    "LLMProvider",
    "LLMResponse",
    "NoLLMConfigError",
    "check_llm_availability",
    "get_api_key",
    "get_available_provider",
    "get_available_providers",
    "get_embeddings",
    "get_llm_config",
    "is_provider_available",
    "route_request",
    # Suggestions
    "Suggestion",
    "SuggestionCategory",
    "SuggestionResult",
    "SuggestionSeverity",
    "get_suggestion_preview",
    "suggest_improvements",
    "suggest_improvements_sync",
    # Summarization
    "SessionSummary",
    "get_summary_preview",
    "summarize_session",
    "summarize_session_sync",
    "summarize_sessions_batch",
]
