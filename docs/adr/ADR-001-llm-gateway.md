# ADR-001: LLM gateway

**Status:** Accepted (Phase 1) · 2026-11-08

**Decision:** Every model call goes through a `ModelProviderAdapter` (`gateway/base.py`). The first two
adapters are `mock` (deterministic, for tests and local runs) and `litellm` (the LiteLLM Python SDK called
in-process). The engine never imports a provider SDK directly.

**Why:** LiteLLM covers OpenAI, Anthropic, Gemini, DeepSeek and the other planned providers behind one call
shape and reports usage and cost. Keeping it behind our own interface means we can add an OpenRouter or
direct-SDK adapter, or move to the LiteLLM *Proxy* as a separate container, without touching the engine.

**Consequences:**
- Provider config is a TOML registry with capabilities, cost metadata, and a credential *reference* (the env
  var name). Inline keys are rejected at load time.
- Model IDs are config, not code. They must be verified against provider docs when a provider is enabled.
- Errors are mapped to retryable (timeouts, rate limits, 5xx) and non-retryable (auth, bad request). The raw
  exception text is not stored because it can echo request details.
- Not yet verified against a live provider.
