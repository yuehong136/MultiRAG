"""Lightweight Channel policy for keeping model reasoning private."""

from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*$", flags=re.IGNORECASE | re.DOTALL)
_OPEN_MARKER = "<think>"
_CLOSE_MARKER = "</think>"


def strip_reasoning(text: str) -> str:
    """Return the user-visible portion of complete or partial think blocks."""

    without_blocks = _THINK_BLOCK_RE.sub("", text)
    without_unclosed = _UNCLOSED_THINK_RE.sub("", without_blocks)
    return without_unclosed.replace("</think>", "").strip()


class StreamingReasoningFilter:
    """Remove ``<think>`` blocks even when markers span delta frames."""

    def __init__(self) -> None:
        self._pending = ""
        self._in_reasoning = False

    def feed(self, content: str) -> str:
        """Return the safe portion that can be emitted without future context."""

        self._pending += content
        visible: list[str] = []
        while self._pending:
            lowered = self._pending.lower()
            if self._in_reasoning:
                closing_at = lowered.find(_CLOSE_MARKER)
                if closing_at >= 0:
                    self._pending = self._pending[closing_at + len(_CLOSE_MARKER) :]
                    self._in_reasoning = False
                    continue
                keep = self._partial_marker_suffix(_CLOSE_MARKER)
                self._pending = self._pending[-keep:] if keep else ""
                break

            opening_at = lowered.find(_OPEN_MARKER)
            closing_at = lowered.find(_CLOSE_MARKER)
            marker_at, marker = self._next_marker(opening_at, closing_at)
            if marker is not None:
                visible.append(self._pending[:marker_at])
                self._pending = self._pending[marker_at + len(marker) :]
                if marker == _OPEN_MARKER:
                    self._in_reasoning = True
                continue

            keep = max(
                self._partial_marker_suffix(_OPEN_MARKER),
                self._partial_marker_suffix(_CLOSE_MARKER),
            )
            visible.append(self._pending[:-keep] if keep else self._pending)
            self._pending = self._pending[-keep:] if keep else ""
            break
        return "".join(visible)

    def finish(self) -> str:
        """Flush a harmless partial marker, or discard an unclosed think block."""

        if self._in_reasoning:
            self._pending = ""
            return ""
        visible = self._pending
        self._pending = ""
        return visible

    def _partial_marker_suffix(self, marker: str) -> int:
        lowered = self._pending.lower()
        for length in range(min(len(lowered), len(marker) - 1), 0, -1):
            if marker.startswith(lowered[-length:]):
                return length
        return 0

    @staticmethod
    def _next_marker(opening_at: int, closing_at: int) -> tuple[int, str | None]:
        if opening_at < 0:
            return closing_at, _CLOSE_MARKER if closing_at >= 0 else None
        if closing_at < 0 or opening_at < closing_at:
            return opening_at, _OPEN_MARKER
        return closing_at, _CLOSE_MARKER
