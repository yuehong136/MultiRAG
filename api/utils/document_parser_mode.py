"""Pure selection plans, before authorization and generation reset.

A pipeline ID here proves syntax only. Callers must check its existence,
DataFlow category, DSL and write authorization before mutation.
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Literal

from common.constants import ParserType


class Unset(Enum):
    TOKEN = "omitted"


UNSET = Unset.TOKEN
BUILTIN_PARSERS = frozenset(parser.value for parser in ParserType)


@dataclass(frozen=True)
class ParserModePlan:
    mode: Literal["builtin", "pipeline"]
    parser_id: str
    pipeline_id: str
    reset_needed: bool


def _required_parser(file_type: str, filename: str) -> str | None:
    suffix = PurePath(filename.lower()).suffix
    if file_type == "visual" or suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
        ".svg",
        ".ico",
        ".mp4",
        ".mov",
        ".avi",
        ".flv",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
        ".3gp",
        ".3gpp",
        ".mkv",
    }:
        return "picture"
    if file_type == "aural" or suffix in {".mp3", ".wav", ".aac", ".flac", ".ogg", ".aiff", ".au", ".midi", ".wma", ".da", ".wave", ".realaudio", ".vqf", ".oggvorbis", ".ape"}:
        return "audio"
    if suffix in {".ppt", ".pptx", ".pages"}:
        return "presentation"
    if suffix in {".eml", ".msg"}:
        return "email"
    return None


def resolve_parser_mode(
    current_builtin: str | None,
    current_pipeline: str | None,
    *,
    chunk_method: object = UNSET,
    pipeline_id: object = UNSET,
    file_type: str = "",
    filename: str = "",
) -> ParserModePlan:
    """Omission retains mode; empty pipeline or explicit builtin leaves pipeline.

    Raw requested values are typed as object so this boundary can reject invalid
    JSON types with ValueError before resource access (including under beartype).
    A builtin switches out even when its ID equals the retained builtin.
    Config-only/non-parser PATCH omit both. Special files permit their automatic
    builtin or pipeline selection; pipeline-only bypasses builtin validation.
    """
    if not isinstance(current_builtin, str) or current_builtin not in BUILTIN_PARSERS | {"general"}:
        raise ValueError("current document has no supported builtin parser")
    if current_pipeline is not None and not isinstance(current_pipeline, str):
        raise ValueError("current pipeline must be a string or None")
    old_pipeline = current_pipeline or ""
    if chunk_method is not UNSET and (not isinstance(chunk_method, str) or chunk_method not in BUILTIN_PARSERS):
        raise ValueError("chunk_method must be a supported builtin parser")
    if pipeline_id is not UNSET and (not isinstance(pipeline_id, str) or (pipeline_id != "" and re.fullmatch(r"[0-9a-f]{32}", pipeline_id) is None)):
        raise ValueError("pipeline_id must be empty or 32 lowercase hex characters")
    if isinstance(pipeline_id, str) and pipeline_id and chunk_method is not UNSET:
        raise ValueError("nonempty pipeline_id conflicts with chunk_method")
    parser = chunk_method if isinstance(chunk_method, str) else current_builtin
    pipeline = old_pipeline
    if isinstance(pipeline_id, str):
        pipeline = pipeline_id
    elif chunk_method is not UNSET:
        pipeline = ""
    if not pipeline and (chunk_method is not UNSET or pipeline_id is not UNSET):
        required = _required_parser(file_type, filename)
        if required is not None and parser != required:
            raise ValueError(f"this document requires the {required} builtin parser")
    return ParserModePlan(
        mode="pipeline" if pipeline else "builtin",
        parser_id=parser,
        pipeline_id=pipeline,
        reset_needed=(parser, pipeline) != (current_builtin, old_pipeline),
    )
