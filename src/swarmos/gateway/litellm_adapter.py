"""LiteLLM-backed adapter (ADR-001): one interface for OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter and
any OpenAI-compatible endpoint. Model IDs come from the user's config."""

from __future__ import annotations

import os
import time

from swarmos.gateway.base import CompletionRequest, CompletionResponse, ProviderConfig, ProviderError


# Use LiteLLM's bundled price table instead of downloading one at import time (slow, and needs network).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def fill_prices(config: ProviderConfig) -> None:
    """If the user gave no prices, take them from LiteLLM's table so budget checks are realistic."""
    if config.cost_per_1k_input is not None and config.cost_per_1k_output is not None:
        return
    try:
        import litellm

        info = litellm.get_model_info(config.model)
        cin, cout = info.get("input_cost_per_token"), info.get("output_cost_per_token")
    except Exception:
        return  # unknown model: the engine reserves each task's cost ceiling instead
    if cin is not None and cout is not None:
        config.cost_per_1k_input, config.cost_per_1k_output = cin * 1000, cout * 1000


class LiteLLMAdapter:
    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        fill_prices(config)

    def _api_key(self) -> str | None:
        if not self.config.api_key_env:
            return None
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ProviderError(
                f"credential reference {self.config.api_key_env} is not set",
                retryable=False,
                kind="missing_credential",
            )
        return key

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            import litellm
        except ImportError as e:  # pragma: no cover - depends on install extras
            raise ProviderError("litellm is not installed (run: uv sync)", retryable=False,
                                kind="missing_dependency") from e

        kwargs: dict = {
            "model": request.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        key = self._api_key()
        if key:
            kwargs["api_key"] = key
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
        if request.json_mode and self.config.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as e:  # map to retryable / non-retryable
            raise _map_error(e) from None

        text = (resp.choices[0].message.content or "") if resp.choices else ""
        finish = getattr(resp.choices[0], "finish_reason", None) if resp.choices else None
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = self.config.estimate_cost(in_tok, out_tok)
        if cost is None:
            try:
                cost = float(litellm.completion_cost(completion_response=resp))
            except Exception:
                cost = None
        return CompletionResponse(
            text=text,
            provider=self.provider_id,
            model=request.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            finish_reason=finish,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


_RETRYABLE = {"Timeout", "RateLimitError", "APIConnectionError", "ServiceUnavailableError",
              "InternalServerError", "APIError"}
_FATAL = {"AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError",
          "ContextWindowExceededError", "ContentPolicyViolationError", "UnsupportedParamsError"}


def _map_error(e: Exception) -> ProviderError:
    names = {c.__name__ for c in type(e).__mro__}
    # Don't echo raw exception text: provider errors can include request details.
    kind = type(e).__name__
    if names & _FATAL:
        return ProviderError(f"provider rejected request ({kind})", retryable=False, kind=kind)
    if names & _RETRYABLE:
        return ProviderError(f"transient provider error ({kind})", retryable=True, kind=kind)
    return ProviderError(f"unexpected provider error ({kind})", retryable=True, kind=kind)
