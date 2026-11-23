"""Audit the complete request consumed by a startup evaluation, without clipping.

This is an evaluation instrument, not a production chat builder. Counts use the
caller's named tokenizer as an estimate; provider usage and billing stay separate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

COMPONENTS = ("system", "question", "history", "startup", "retrieval")
OUTPUT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "max_new_tokens")
MAX_MESSAGES = 256
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def inspect_answer_request(
    request: Mapping[str, Any],
    *,
    mode: str,
    context_window_tokens: int,
    output_reserve_tokens: int,
    safety_margin_tokens: int,
    count_tokens: Callable[[str], int],
    tokenizer_label: str,
    components: Mapping[str, list[int]],
    startup_domain: Mapping[str, Any] | None = None,
    startup_representation: str | None = None,
) -> dict[str, Any]:
    """Return a bounded decision and fingerprint for the exact supplied request.

    Every message belongs to one diagnostic component. All other request fields,
    including tools/options and JSON framing, remain in the full request count.
    The helper never removes retrieval merely because startup data is present.
    """
    if mode not in {"baseline", "startup"} or not isinstance(tokenizer_label, str) or not tokenizer_label.strip():
        raise ValueError("invalid_answer_budget_identity")
    for value, minimum in ((context_window_tokens, 1), (output_reserve_tokens, 1), (safety_margin_tokens, 0)):
        if type(value) is not int or value < minimum:
            raise ValueError("invalid_answer_budget_limit")
    model = request.get("model")
    messages = request.get("messages")
    if not isinstance(model, str) or not model or not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("invalid_answer_request")
    if any(not isinstance(message, dict) or not isinstance(message.get("role"), str) or "content" not in message for message in messages):
        raise ValueError("invalid_answer_messages")
    if set(components) != set(COMPONENTS):
        raise ValueError("invalid_answer_components")
    indexes = [index for key in COMPONENTS for index in components[key]]
    if any(type(index) is not int or not 0 <= index < len(messages) for index in indexes) or sorted(indexes) != list(range(len(messages))):
        raise ValueError("answer_components_must_partition_messages")
    if not components["system"] or not components["question"]:
        raise ValueError("answer_system_and_question_required")
    encoded = _json(dict(request))
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("answer_request_byte_limit")

    def estimate(value: str) -> int:
        count = count_tokens(value)
        if type(count) is not int or count < 0 or (value and count == 0):
            raise ValueError("invalid_answer_token_estimate")
        return count

    startup: dict[str, Any] | None = None
    if mode == "baseline":
        if components["startup"] or startup_domain is not None or startup_representation is not None:
            raise ValueError("baseline_cannot_contain_startup")
    else:
        if startup_domain is None or startup_representation not in {"json", "text"} or len(components["startup"]) != 1:
            raise ValueError("startup_consumption_required")
        if startup_representation == "json":
            expected = _json(dict(startup_domain))
        else:
            text_content = startup_domain.get("startup_text")
            if not isinstance(text_content, str):
                raise ValueError("invalid_startup_text")
            expected = text_content
        actual = messages[components["startup"][0]]["content"]
        # No summary, substring or caller-side truncation can pass as a full pack.
        if actual != expected:
            raise ValueError("startup_consumption_not_complete")
        startup = {
            "representation": startup_representation,
            "package_sha256": startup_domain.get("package_sha256"),
            "domain_estimated_tokens": estimate(_json(dict(startup_domain))),
            "consumed_content_estimated_tokens": estimate(expected),
            "domain_budget_tokens": startup_domain.get("usage", {}).get("budget_tokens"),
            "complete": True,
        }
    caps = [(field, request[field]) for field in OUTPUT_FIELDS if field in request]
    if any(type(value) is not int or value < 1 for _, value in caps):
        raise ValueError("invalid_answer_output_cap")
    reasons: list[str] = []
    if not caps:
        reasons.append("request_output_cap_missing")
    elif len(caps) != 1:
        reasons.append("request_output_cap_ambiguous")
    elif caps[0][1] > output_reserve_tokens:
        reasons.append("request_output_cap_exceeds_reserve")
    total = estimate(encoded)
    required = total + output_reserve_tokens + safety_margin_tokens
    if required > context_window_tokens:
        reasons.append("caller_context_budget_exceeded")
    message_total = estimate(_json(messages))
    return {
        "mode": mode,
        "model": model,
        "request_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "request_bytes": len(encoded.encode("utf-8")),
        "message_count": len(messages),
        "allowed": not reasons,
        "reasons": reasons,
        "estimated": True,
        "tokenizer_label": tokenizer_label,
        "target_tokenizer_exact": False,
        "context_window_tokens": context_window_tokens,
        "request_estimated_tokens": total,
        "output_reserve_tokens": output_reserve_tokens,
        "safety_margin_tokens": safety_margin_tokens,
        "required_estimated_tokens": required,
        "remaining_estimated_tokens": context_window_tokens - required,
        "component_estimated_tokens": {key: sum(estimate(_json(messages[index])) for index in components[key]) for key in COMPONENTS},
        "messages_estimated_tokens": message_total,
        "request_wrapping_estimated_delta": total - message_total,
        "component_counts_are_nonadditive": True,
        "startup_consumption": startup,
        "provider_usage": None,
        "money": None,
        "mutates_request": False,
        "clips_content": False,
        "excludes": ["provider_hidden_protocol", "provider_internal_retries", "unobserved_tool_followups"],
    }
