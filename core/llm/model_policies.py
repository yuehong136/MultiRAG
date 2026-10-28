"""Provider-independent model generation constraints."""

import re
from typing import Any

_CLAUDE_VERSION_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")
_CLAUDE_LEGACY_VERSION_RE = re.compile(r"claude-(\d)(?:-(\d))?-(?:opus|sonnet|haiku)")


def apply_claude_sampling_policy(model_name: str, *targets: dict[str, Any]) -> None:
    """Normalize Anthropic and regional Bedrock names before applying policy."""
    if "claude" not in model_name:
        return
    if any(marker in model_name for marker in ("claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5", "claude-fable", "claude-mythos")):
        for target in targets:
            for key in ("temperature", "top_p", "top_k"):
                target.pop(key, None)
        return
    match = _CLAUDE_VERSION_RE.search(model_name) or _CLAUDE_LEGACY_VERSION_RE.search(model_name)
    if match and (int(match[1]), int(match[2] or 0)) < (4, 1):
        return
    if any("temperature" in target for target in targets) and any("top_p" in target for target in targets):
        for target in targets:
            target.pop("top_p", None)
