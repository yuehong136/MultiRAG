"""
TokenChunker utility wrapper.

This module keeps the analyze_v2 direct-call interface while reusing the
runtime implementation from core.flow.chunker.token_chunker.
"""

import logging
import re

from common.float_utils import normalize_overlapped_percent
from common.misc_utils import thread_pool_exec
from core.flow.chunker.token_chunker import (
    _attach_context_to_media_chunks,
    _build_json_chunks,
    _compile_delimiter_pattern,
    _finalize_json_chunks,
    _merge_text_chunks_by_token_size,
    _split_chunk_docs_by_children,
    _split_text_by_pattern,
)
from core.nlp import naive_merge

logger = logging.getLogger(__name__)


async def _to_thread(func, *args, **kwargs):
    return await thread_pool_exec(func, *args, **kwargs)


def _compile_children_pattern(children_delimiters: list[str] | None) -> str:
    return "|".join(re.escape(text) for text in sorted(set(children_delimiters or []), key=len, reverse=True) if text)


class FlowTokenChunker:
    """Canvas-free TokenChunker facade for analyze_v2 and service utilities."""

    @staticmethod
    async def split_text(
        text: str,
        chunk_token_size: int = 512,
        delimiters: list[str] | None = None,
        overlapped_percent: float | int = 0,
        children_delimiters: list[str] | None = None,
        callback=None,
    ) -> list[dict]:
        if callback:
            callback(0.1, "Start to split into chunks.")

        delimiter_pattern = _compile_delimiter_pattern(delimiters or ["\n"])
        children_pattern = _compile_children_pattern(children_delimiters)
        overlapped_percent = normalize_overlapped_percent(overlapped_percent)
        payload = text or ""

        if delimiter_pattern:
            chunks = _split_text_by_pattern(payload, delimiter_pattern)
        else:
            chunks = await _to_thread(
                naive_merge,
                payload,
                chunk_token_size,
                "",
                overlapped_percent,
            )

        if children_pattern:
            docs = []
            for chunk in chunks:
                if not chunk.strip():
                    continue
                for child_text in _split_text_by_pattern(chunk, children_pattern):
                    if child_text.strip():
                        docs.append({"text": child_text, "mom": chunk})
        else:
            docs = [{"text": chunk.strip()} for chunk in chunks if chunk.strip()]

        if callback:
            callback(1.0, f"Split into {len(docs)} chunks.")

        return docs

    @staticmethod
    async def split_json(
        json_result: list[dict] | None,
        chunk_token_size: int = 512,
        delimiters: list[str] | None = None,
        overlapped_percent: float | int = 0,
        children_delimiters: list[str] | None = None,
        table_context_size: int = 0,
        image_context_size: int = 0,
        callback=None,
    ) -> list[dict]:
        if callback:
            callback(0.1, "Start to split into chunks.")

        delimiter_pattern = _compile_delimiter_pattern(delimiters or ["\n"])
        children_pattern = _compile_children_pattern(children_delimiters)
        overlapped_percent = normalize_overlapped_percent(overlapped_percent)

        chunks = _build_json_chunks(json_result or [], delimiter_pattern)
        _attach_context_to_media_chunks(chunks, table_context_size, image_context_size)

        if not delimiter_pattern:
            chunks = _merge_text_chunks_by_token_size(
                chunks,
                chunk_token_size,
                overlapped_percent,
            )

        if children_pattern:
            chunks = _split_chunk_docs_by_children(chunks, children_pattern)

        docs = _finalize_json_chunks(chunks)
        if callback:
            callback(1.0, f"Split into {len(docs)} chunks.")

        return docs


async def split_chunks(
    parsed_result: dict,
    chunk_token_size: int = 512,
    delimiters: list[str] | None = None,
    overlapped_percent: float | int = 0,
    children_delimiters: list[str] | None = None,
    table_context_size: int = 0,
    image_context_size: int = 0,
    callback=None,
) -> list[dict]:
    """
    Split parser output with the TokenChunker runtime semantics.

    REST/API field names stay unchanged: callers may still pass
    ``splitter_config`` and its existing keys.
    """
    output_format = parsed_result.get("output_format")

    if output_format in {"text", "markdown", "html"}:
        payload = parsed_result.get(output_format, "")
        if not payload or not str(payload).strip():
            logger.warning("Empty %s payload for TokenChunker, keys=%s", output_format, parsed_result.keys())
            return []

        return await FlowTokenChunker.split_text(
            str(payload),
            chunk_token_size,
            delimiters,
            overlapped_percent,
            children_delimiters,
            callback,
        )

    if output_format == "json":
        return await FlowTokenChunker.split_json(
            parsed_result.get("json", []),
            chunk_token_size,
            delimiters,
            overlapped_percent,
            children_delimiters,
            table_context_size,
            image_context_size,
            callback,
        )

    if output_format == "chunks":
        return _finalize_json_chunks(parsed_result.get("chunks", []))

    raise ValueError(f"Unsupported output_format: {output_format}")
