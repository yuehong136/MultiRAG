"""Shared helpers for keeping private model reasoning out of persisted history."""

from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*$", flags=re.IGNORECASE | re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Return only user-visible text from complete or unterminated think blocks."""

    without_blocks = _THINK_BLOCK_RE.sub("", text)
    without_unclosed = _UNCLOSED_THINK_RE.sub("", without_blocks)
    return without_unclosed.replace("</think>", "").strip()
