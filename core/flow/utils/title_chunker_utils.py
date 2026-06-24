# coding=utf-8
"""
TitleChunker utility wrapper.

This module preserves the analyze_v2 chapter/summaries return contract while
delegating title-level resolution and grouping to core.flow.chunker.title_chunker.
"""

import logging
from copy import deepcopy
from types import SimpleNamespace

from core.flow.chunker.title_chunker import TitleChunker, TitleChunkerParam

logger = logging.getLogger(__name__)


DEFAULT_LEVELS = [
    [r"^#\s+", r"^第[一二三四五六七八九十百]+章"],
    [r"^##\s+", r"^\d+\.\s+"],
]


def _make_process(param: TitleChunkerParam, callback=None):
    process = TitleChunker.__new__(TitleChunker)
    process._param = param
    process._canvas = SimpleNamespace(_doc_id=None, _tenant_id="")
    process._id = "title-chunker-utils"

    def _callback(prog, msg=""):
        if callback:
            callback(prog, msg)

    process.callback = _callback
    return process


def _normalize_input_chunk(chunk) -> dict:
    if isinstance(chunk, dict):
        normalized = deepcopy(chunk)
        text = normalized.get("text")
        if not isinstance(text, str):
            text = normalized.get("content_with_weight")
        normalized["text"] = text if isinstance(text, str) else ("" if text is None else str(text))
    else:
        normalized = {"text": "" if chunk is None else str(chunk)}

    normalized.setdefault("content_with_weight", normalized.get("text", ""))
    normalized.setdefault("doc_type_kwd", "text")
    return normalized


def _normalize_output_chunk(chunk: dict) -> dict:
    normalized = deepcopy(chunk)
    text = normalized.get("text")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    normalized["text"] = text
    normalized.setdefault("content_with_weight", text)
    normalized.setdefault("content_ltks", text)
    normalized.setdefault("doc_type_kwd", "text")
    return normalized


def _title_from_text(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _match_source_indices(output_text: str, input_chunks: list[dict], start_index: int) -> tuple[list[int], int]:
    indices = []
    output_text = output_text or ""

    for index in range(start_index, len(input_chunks)):
        source_text = str(input_chunks[index].get("text") or "").strip()
        if not source_text:
            continue

        if source_text in output_text:
            indices.append(index)
            continue

        if indices:
            return indices, index

    if indices:
        return indices, indices[-1] + 1

    fallback = min(start_index, len(input_chunks) - 1)
    return ([fallback] if fallback >= 0 else []), fallback + 1


class FlowTitleChunker:
    """Canvas-free TitleChunker facade for analyze_v2 and service utilities."""

    @staticmethod
    async def merge(
        chunks: list[dict],
        levels: list[list[str]] | None = None,
        hierarchy: int = 1,
        callback=None,
        method: str = "hierarchy",
        include_heading_content: bool = False,
        file: dict | None = None,
    ) -> dict:
        param = TitleChunkerParam()
        param.method = method or "hierarchy"
        param.levels = DEFAULT_LEVELS if levels is None else levels
        param.hierarchy = hierarchy if hierarchy is not None else 1
        param.include_heading_content = bool(include_heading_content)

        process = _make_process(param, callback)
        input_chunks = [_normalize_input_chunk(chunk) for chunk in chunks or []]

        await process._invoke(
            name=(file or {}).get("name", "utils.md"),
            file=file or {"outlines": []},
            output_format="chunks",
            chunks=input_chunks,
        )

        error = process.output("_ERROR")
        if error:
            raise ValueError(error)

        output_chunks = [_normalize_output_chunk(chunk) for chunk in (process.output("chunks") or [])]
        chapters = []
        cursor = 0
        for output_chunk in output_chunks:
            indices, cursor = _match_source_indices(output_chunk["text"], input_chunks, cursor)
            chapter_chunks = [input_chunks[index] for index in indices]
            chapters.append(
                {
                    "title": _title_from_text(output_chunk["text"]),
                    "text": output_chunk["text"],
                    "chunks": chapter_chunks,
                    "chunk_indices": indices,
                    "doc_type_kwd": output_chunk.get("doc_type_kwd", "text"),
                    "position_int": output_chunk.get("position_int", []),
                    "page_num_int": output_chunk.get("page_num_int", []),
                    "top_int": output_chunk.get("top_int", []),
                    "img_id": output_chunk.get("img_id"),
                    "image": output_chunk.get("image"),
                }
            )

        if callback:
            callback(1.0, f"Merged into {len(chapters)} chapters.")

        logger.info("TitleChunker: %s chapters identified", len(chapters))
        return {
            "chapters": chapters,
            "summaries": [chapter["text"] for chapter in chapters],
        }


async def hierarchical_merge(
    chunks: list[dict],
    levels: list[list[str]] | None = None,
    hierarchy: int = 1,
    callback=None,
    method: str = "hierarchy",
    include_heading_content: bool = False,
    file: dict | None = None,
) -> dict:
    return await FlowTitleChunker.merge(
        chunks,
        levels,
        hierarchy,
        callback,
        method,
        include_heading_content,
        file,
    )
