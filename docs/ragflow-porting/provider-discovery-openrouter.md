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

OpenRouter uses `chat/completions` for normal and streamed requests, including
Qwen and GLM model names. It preserves role history and content parts, maps prior
reasoning to `reasoning`, and reads that field in normal and SSE replies. Explicit
thinking settings use the `reasoning` object; an explicit false value takes
precedence over effort. Calls preserve cancellation and sender errors, reject
malformed/business-error responses, and report success only after an answer and
valid stream termination. Driver instances clone their address maps and share
only the connection pool.

The Go service and bound chat model use `ChatWithMessages` and sender streaming.
The obsolete concrete MiniMax single-message wrapper has no runtime consumer;
its removal retains the role-history and sender paths used by the service.

NVIDIA discovery and connection checks use the configured NIM `models` endpoint.
Discovery permits an absent API key for self-hosted deployments; chat continues
to require a key. Missing or malformed lists and provider/HTTP errors fail,
while an explicitly empty inventory is valid. Discovery remains request-scoped,
cancellable, and bounded by the normal call timeout.
