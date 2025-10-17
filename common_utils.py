"""
This file contains common utils for anthropic calls.
"""

from typing import Any, Dict, List, Optional, Union

import httpx

import litellm
from litellm.litellm_core_utils.prompt_templates.common_utils import (
    get_file_ids_from_messages,
)
from litellm.llms.base_llm.base_utils import BaseLLMModelInfo, BaseTokenCounter
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.types.llms.anthropic import AllAnthropicToolsValues, AnthropicMcpServerTool
from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import TokenCountResponse


class AnthropicError(BaseLLMException):
    def __init__(
        self,
        status_code: int,
        message,
        headers: Optional[httpx.Headers] = None,
    ):
        super().__init__(status_code=status_code, message=message, headers=headers)


class AnthropicModelInfo(BaseLLMModelInfo):
    def is_cache_control_set(self, messages: List[AllMessageValues]) -> bool:
        """
        Return if {"cache_control": ..} in message content block

        Used to check if anthropic prompt caching headers need to be set.
        """
        for message in messages:
            if message.get("cache_control", None) is not None:
                return True
            _message_content = message.get("content")
            if _message_content is not None and isinstance(_message_content, list):
                for content in _message_content:
                    if "cache_control" in content:
                        return True

        return False

    def is_file_id_used(self, messages: List[AllMessageValues]) -> bool:
        """
        Return if {"source": {"type": "file", "file_id": ..}} in message content block
        """
        file_ids = get_file_ids_from_messages(messages)
        return len(file_ids) > 0

    def is_mcp_server_used(
        self, mcp_servers: Optional[List[AnthropicMcpServerTool]]
    ) -> bool:
        if mcp_servers is None:
            return False
        if mcp_servers:
            return True
        return False

    def is_computer_tool_used(
        self, tools: Optional[List[AllAnthropicToolsValues]]
    ) -> bool:
        if tools is None:
            return False
        for tool in tools:
            if "type" in tool and tool["type"].startswith("computer_"):
                return True
        return False

    def is_pdf_used(self, messages: List[AllMessageValues]) -> bool:
        """
        Set to true if media passed into messages.

        """
        for message in messages:
            if (
                "content" in message
                and message["content"] is not None
                and isinstance(message["content"], list)
            ):
                for content in message["content"]:
                    if "type" in content and content["type"] != "text":
                        return True
        return False

    def _get_user_anthropic_beta_headers(
        self, anthropic_beta_header: Optional[str]
    ) -> Optional[List[str]]:
        if anthropic_beta_header is None:
            return None
        return anthropic_beta_header.split(",")

    @staticmethod
    def detect_oauth_token(auth_header: str) -> bool:
        """Detect if authorization header contains OAuth token vs API key"""
        import logging
        logger = logging.getLogger(__name__)

        logger.info(f"[OAuth Debug] detect_oauth_token called with header: {auth_header[:50]}...")

        if not auth_header.startswith("Bearer "):
            logger.info(f"[OAuth Debug] Header does not start with 'Bearer ', returning False")
            return False

        token = auth_header.replace("Bearer ", "")
        is_oauth = token.startswith("sk-ant-oat")

        logger.info(f"[OAuth Debug] Token starts with: {token[:15]}...")
        logger.info(f"[OAuth Debug] Is OAuth token: {is_oauth}")

        return is_oauth

    @staticmethod
    def extract_oauth_from_request(request_headers: dict, litellm_params: dict) -> Optional[str]:
        """Extract OAuth token from request headers if pass-through enabled"""
        import logging
        logger = logging.getLogger(__name__)

        logger.info(f"[OAuth Debug] extract_oauth_from_request called")
        logger.info(f"[OAuth Debug] Headers keys: {list(request_headers.keys())}")
        logger.info(f"[OAuth Debug] oauth_pass_through: {litellm_params.get('oauth_pass_through', False)}")

        if not litellm_params.get("oauth_pass_through", False):
            logger.info(f"[OAuth Debug] oauth_pass_through is False, returning None")
            return None

        # Handle case-insensitive header lookup
        auth_header = request_headers.get("authorization") or request_headers.get("Authorization", "")
        logger.info(f"[OAuth Debug] Found auth header: {auth_header[:50] if auth_header else 'None'}...")

        if AnthropicModelInfo.detect_oauth_token(auth_header):
            token = auth_header.replace("Bearer ", "")
            logger.info(f"[OAuth Debug] Extracted OAuth token: {token[:15]}...")
            return token

        logger.info(f"[OAuth Debug] No OAuth token detected, returning None")
        return None

    def get_anthropic_headers(
        self,
        api_key: str,
        anthropic_version: Optional[str] = None,
        computer_tool_used: bool = False,
        prompt_caching_set: bool = False,
        pdf_used: bool = False,
        file_id_used: bool = False,
        mcp_server_used: bool = False,
        is_vertex_request: bool = False,
        user_anthropic_beta_headers: Optional[List[str]] = None,
        oauth_token: Optional[str] = None,
    ) -> dict:
        betas = set()
        if prompt_caching_set:
            betas.add("prompt-caching-2024-07-31")
        if computer_tool_used:
            betas.add("computer-use-2024-10-22")
        # if pdf_used:
        #     betas.add("pdfs-2024-09-25")
        if file_id_used:
            betas.add("files-api-2025-04-14")
            betas.add("code-execution-2025-05-22")
        if mcp_server_used:
            betas.add("mcp-client-2025-04-04")

        headers = {
            "anthropic-version": anthropic_version or "2023-06-01",
            "accept": "application/json",
            "content-type": "application/json",
        }

        # OAuth authentication vs API key authentication
        import logging
        logger = logging.getLogger(__name__)

        if oauth_token:
            logger.info(f"[OAuth Debug] Using OAuth authentication with token: {oauth_token[:15]}...")
            headers["authorization"] = f"Bearer {oauth_token}"
            # Add OAuth beta headers from OpenCode research
            betas.update({"oauth-2025-04-20", "claude-code-20250219", "interleaved-thinking-2025-05-14", "fine-grained-tool-streaming-2025-05-14"})
        else:
            logger.info(f"[OAuth Debug] Using API key authentication with key: {api_key[:15] if api_key else 'None'}...")
            # Existing API key authentication
            headers["x-api-key"] = api_key

        if user_anthropic_beta_headers is not None:
            betas.update(user_anthropic_beta_headers)

        # Don't send any beta headers to Vertex, Vertex has failed requests when they are sent
        if is_vertex_request is True:
            pass
        elif len(betas) > 0:
            headers["anthropic-beta"] = ",".join(betas)

        return headers

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> Dict:
        # OAuth authentication logic - supports multiple OAuth token sources
        import logging
        logger = logging.getLogger(__name__)

        oauth_token = None
        logger.info(f"[OAuth Debug] validate_environment called for model: {model}")
        logger.info(f"[OAuth Debug] api_key provided: {api_key[:20] if api_key else 'None'}...")

        # Method 1: Check if api_key is actually an OAuth token (set by our litellm_pre_call_utils)
        if api_key and api_key.startswith("sk-ant-oat"):
            logger.info(f"[OAuth Debug] api_key is an OAuth token, using it as oauth_token")
            oauth_token = api_key
            api_key = None  # Clear api_key so we don't use it incorrectly

        # Method 2: Check if OAuth token is in litellm_params (working ARM image approach)
        elif litellm_params.get("oauth_pass_through") and litellm_params.get("oauth_token"):
            logger.info(f"[OAuth Debug] Found oauth_token in litellm_params")
            oauth_token = litellm_params.get("oauth_token")

        # Method 3: Extract from the headers parameter passed to this method
        elif litellm_params.get("oauth_pass_through"):
            logger.info(f"[OAuth Debug] Extracting from headers parameter")
            oauth_token = self.extract_oauth_from_request(headers, litellm_params)
            logger.info(f"[OAuth Debug] OAuth from headers: {'Found' if oauth_token else 'None'}")

        # Method 4: Extract from request headers in litellm_params
        else:
            logger.info(f"[OAuth Debug] Checking litellm_params for request headers")
            request_headers = {}
            if "proxy_server_request" in litellm_params and "headers" in litellm_params["proxy_server_request"]:
                request_headers = litellm_params["proxy_server_request"]["headers"]
                logger.info(f"[OAuth Debug] Got headers from proxy_server_request")
            elif "secret_fields" in litellm_params and hasattr(litellm_params["secret_fields"], "raw_headers"):
                request_headers = litellm_params["secret_fields"].raw_headers
                logger.info(f"[OAuth Debug] Got headers from secret_fields.raw_headers")

            if request_headers:
                logger.info(f"[OAuth Debug] Request headers keys: {list(request_headers.keys())}")
                oauth_token = self.extract_oauth_from_request(request_headers, litellm_params)
                logger.info(f"[OAuth Debug] OAuth from request headers: {'Found' if oauth_token else 'None'}")

        # Fallback to API key authentication
        if oauth_token is None and api_key is None:
            raise litellm.AuthenticationError(
                message="Missing Anthropic API Key - A call is being made to anthropic but no key is set either in the environment variables or via params. Please set `ANTHROPIC_API_KEY` in your environment vars",
                llm_provider="anthropic",
                model=model,
            )

        tools = optional_params.get("tools")
        prompt_caching_set = self.is_cache_control_set(messages=messages)
        computer_tool_used = self.is_computer_tool_used(tools=tools)
        mcp_server_used = self.is_mcp_server_used(
            mcp_servers=optional_params.get("mcp_servers")
        )
        pdf_used = self.is_pdf_used(messages=messages)
        file_id_used = self.is_file_id_used(messages=messages)
        user_anthropic_beta_headers = self._get_user_anthropic_beta_headers(
            anthropic_beta_header=headers.get("anthropic-beta")
        )
        anthropic_headers = self.get_anthropic_headers(
            computer_tool_used=computer_tool_used,
            prompt_caching_set=prompt_caching_set,
            pdf_used=pdf_used,
            api_key=api_key or "",  # Provide empty string if None to avoid errors
            file_id_used=file_id_used,
            is_vertex_request=optional_params.get("is_vertex_request", False),
            user_anthropic_beta_headers=user_anthropic_beta_headers,
            mcp_server_used=mcp_server_used,
            oauth_token=oauth_token,
        )

        headers = {**headers, **anthropic_headers}

        return headers

    @staticmethod
    def get_api_base(api_base: Optional[str] = None) -> Optional[str]:
        from litellm.secret_managers.main import get_secret_str

        return (
            api_base
            or get_secret_str("ANTHROPIC_API_BASE")
            or "https://api.anthropic.com"
        )

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        from litellm.secret_managers.main import get_secret_str

        return api_key or get_secret_str("ANTHROPIC_API_KEY")

    @staticmethod
    def get_base_model(model: Optional[str] = None) -> Optional[str]:
        return model.replace("anthropic/", "") if model else None

    def get_models(
        self, api_key: Optional[str] = None, api_base: Optional[str] = None
    ) -> List[str]:
        api_base = AnthropicModelInfo.get_api_base(api_base)
        api_key = AnthropicModelInfo.get_api_key(api_key)
        if api_base is None or api_key is None:
            raise ValueError(
                "ANTHROPIC_API_BASE or ANTHROPIC_API_KEY is not set. Please set the environment variable, to query Anthropic's `/models` endpoint."
            )
        response = litellm.module_level_client.get(
            url=f"{api_base}/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise Exception(
                f"Failed to fetch models from Anthropic. Status code: {response.status_code}, Response: {response.text}"
            )

        models = response.json()["data"]

        litellm_model_names = []
        for model in models:
            stripped_model_name = model["id"]
            litellm_model_name = "anthropic/" + stripped_model_name
            litellm_model_names.append(litellm_model_name)
        return litellm_model_names

    def get_token_counter(self) -> Optional[BaseTokenCounter]:
        """
        Factory method to create an Anthropic token counter.

        Returns:
            AnthropicTokenCounter instance for this provider.
        """
        return AnthropicTokenCounter()


class AnthropicTokenCounter(BaseTokenCounter):
    """Token counter implementation for Anthropic provider."""

    def should_use_token_counting_api(
        self,
        custom_llm_provider: Optional[str] = None,
    ) -> bool:
        from litellm.types.utils import LlmProviders
        return custom_llm_provider == LlmProviders.ANTHROPIC.value

    async def count_tokens(
        self,
        model_to_use: str,
        messages: Optional[List[Dict[str, Any]]],
        contents: Optional[List[Dict[str, Any]]],
        deployment: Optional[Dict[str, Any]] = None,
        request_model: str = "",
    ) -> Optional[TokenCountResponse]:
        from litellm.proxy.utils import count_tokens_with_anthropic_api

        result = await count_tokens_with_anthropic_api(
            model_to_use=model_to_use,
            messages=messages,
            deployment=deployment,
        )

        if result is not None:
            return TokenCountResponse(
                total_tokens=result.get("total_tokens", 0),
                request_model=request_model,
                model_used=model_to_use,
                tokenizer_type=result.get("tokenizer_used", ""),
                original_response=result,
            )

        return None


def process_anthropic_headers(headers: Union[httpx.Headers, dict]) -> dict:
    openai_headers = {}
    if "anthropic-ratelimit-requests-limit" in headers:
        openai_headers["x-ratelimit-limit-requests"] = headers[
            "anthropic-ratelimit-requests-limit"
        ]
    if "anthropic-ratelimit-requests-remaining" in headers:
        openai_headers["x-ratelimit-remaining-requests"] = headers[
            "anthropic-ratelimit-requests-remaining"
        ]
    if "anthropic-ratelimit-tokens-limit" in headers:
        openai_headers["x-ratelimit-limit-tokens"] = headers[
            "anthropic-ratelimit-tokens-limit"
        ]
    if "anthropic-ratelimit-tokens-remaining" in headers:
        openai_headers["x-ratelimit-remaining-tokens"] = headers[
            "anthropic-ratelimit-tokens-remaining"
        ]

    llm_response_headers = {
        "{}-{}".format("llm_provider", k): v for k, v in headers.items()
    }

    additional_headers = {**llm_response_headers, **openai_headers}
    return additional_headers
