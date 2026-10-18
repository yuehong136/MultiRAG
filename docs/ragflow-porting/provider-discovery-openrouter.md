# Go provider discovery and OpenRouter

The Go provider service creates a driver for each configured provider instance.
Ollama, LM Studio, and vLLM connection checks issue a model discovery request to
that instance's configured address. Local API keys are optional; an omitted or
empty region selects `default`. A named, unconfigured region fails explicitly
instead of silently using a different endpoint. Missing addresses, transport and
HTTP failures, provider errors, and malformed model lists fail the check.

Ollama retains its native `api/tags` discovery and NDJSON chat protocol. LM Studio
and vLLM use the configured OpenAI-compatible model-list suffix. vLLM retains its
existing requirement that at least one model be available.
