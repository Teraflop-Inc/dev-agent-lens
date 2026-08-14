"""Force thinking summaries on adaptive-thinking requests (ENG2-1487).

Anthropic never returns raw chain-of-thought; `thinking.display` decides whether
you get a readable summary ("summarized") or empty-text thinking blocks
("omitted", the default every client sends). Fleet decision 2026-08-14 (Adam):
summaries ON — so reasoning text lands in Phoenix instead of empty blocks.

Scope guards:
- Only touches `thinking: {"type": "adaptive"}` requests. `disabled` stays
  disabled (also: display on a disabled block is invalid), and pre-4.6
  `{"type": "enabled", budget_tokens: N}` requests are left alone — `display`
  doesn't exist on those models and could 400.
- Overwrites an explicit `display: "omitted"` deliberately: that's client
  plumbing default, and the fleet decision is summaries on.

Registered in the litellm config as:
    callbacks: ["arize_phoenix", "thinking_display_hook.proxy_handler_instance"]
The module must sit next to the proxy's cwd (/app in both the fly image and the
compose containers).

Rollback: remove the callback entry, restart the proxy.
"""

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger


class ThinkingDisplayHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            thinking = data.get("thinking") if isinstance(data, dict) else None
            if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
                if thinking.get("display") != "summarized":
                    data["thinking"] = {**thinking, "display": "summarized"}
                    verbose_proxy_logger.debug(
                        "[thinking_display_hook] set display=summarized "
                        "(was %r, call_type=%s)",
                        thinking.get("display"),
                        call_type,
                    )
        except Exception as exc:  # never break a request over an observability knob
            verbose_proxy_logger.warning("[thinking_display_hook] skipped: %s", exc)
        return data


proxy_handler_instance = ThinkingDisplayHook()
