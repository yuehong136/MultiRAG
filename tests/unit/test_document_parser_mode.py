"""Selection transitions, omission and special files without service side effects."""

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from api.utils.document_parser_mode import UNSET, ParserModePlan, resolve_parser_mode

A = "a" * 32
B = "b" * 32


@pytest.mark.parametrize(
    ("builtin", "pipeline", "selection", "expected"),
    [
        ("naive", None, {}, ParserModePlan("builtin", "naive", "", False)),
        ("naive", "", {"chunk_method": "naive"}, ParserModePlan("builtin", "naive", "", False)),
        ("naive", "", {"pipeline_id": ""}, ParserModePlan("builtin", "naive", "", False)),
        ("naive", "", {"chunk_method": "manual"}, ParserModePlan("builtin", "manual", "", True)),
        ("naive", A, {}, ParserModePlan("pipeline", "naive", A, False)),
        ("naive", A, {"pipeline_id": A}, ParserModePlan("pipeline", "naive", A, False)),
        ("naive", A, {"pipeline_id": B}, ParserModePlan("pipeline", "naive", B, True)),
        ("naive", "", {"pipeline_id": A}, ParserModePlan("pipeline", "naive", A, True)),
        ("naive", A, {"pipeline_id": ""}, ParserModePlan("builtin", "naive", "", True)),
        ("naive", A, {"chunk_method": "naive"}, ParserModePlan("builtin", "naive", "", True)),
        ("naive", A, {"chunk_method": "resume"}, ParserModePlan("builtin", "resume", "", True)),
        ("manual", A, {"pipeline_id": "", "chunk_method": "resume"}, ParserModePlan("builtin", "resume", "", True)),
        ("resume", "", {"chunk_method": "resume"}, ParserModePlan("builtin", "resume", "", False)),
        ("general", A, {}, ParserModePlan("pipeline", "general", A, False)),
        ("general", "", {"pipeline_id": ""}, ParserModePlan("builtin", "general", "", False)),
    ],
)
def test_mode_transition_matrix(builtin: str, pipeline: str | None, selection: dict[str, Any], expected: ParserModePlan) -> None:
    assert resolve_parser_mode(builtin, pipeline, **selection) == expected


@pytest.mark.parametrize(
    "selection",
    [
        {"chunk_method": None},
        {"pipeline_id": None},
        {"chunk_method": True},
        {"pipeline_id": True},
        {"chunk_method": ""},
        {"chunk_method": "general"},
        {"chunk_method": "made-up"},
        {"chunk_method": "NAIVE"},
        {"pipeline_id": " "},
        {"pipeline_id": "A" * 32},
        {"pipeline_id": "a" * 31},
        {"pipeline_id": "g" * 32},
        {"pipeline_id": "general"},
        {"pipeline_id": A, "chunk_method": "naive"},
    ],
)
def test_bad_selection_and_conflicts_are_rejected(selection: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        resolve_parser_mode("naive", "", **selection)


@pytest.mark.parametrize(
    ("filename", "file_type", "parser"),
    [
        ("SLIDES.PPT", "", "presentation"),
        ("slides.PptX", "", "presentation"),
        ("slides.Pages", "", "presentation"),
        ("IMAGE.PNG", "", "picture"),
        ("no_extension", "visual", "picture"),
        ("movie.MP4", "", "picture"),
        ("record.MP3", "", "audio"),
        ("no_extension", "aural", "audio"),
        ("letter.EML", "", "email"),
        ("letter.MSG", "", "email"),
    ],
)
def test_special_documents_can_keep_original_select_pipeline_and_return(filename: str, file_type: str, parser: str) -> None:
    context = {"filename": filename, "file_type": file_type}
    assert resolve_parser_mode(parser, "", chunk_method=parser, **context) == ParserModePlan("builtin", parser, "", False)
    assert resolve_parser_mode(parser, "", pipeline_id=A, **context) == ParserModePlan("pipeline", parser, A, True)
    assert resolve_parser_mode(parser, A, pipeline_id=A, **context) == ParserModePlan("pipeline", parser, A, False)
    assert resolve_parser_mode(parser, A, pipeline_id="", **context) == ParserModePlan("builtin", parser, "", True)
    assert resolve_parser_mode(parser, A, chunk_method=parser, **context) == ParserModePlan("builtin", parser, "", True)
    with pytest.raises(ValueError, match="requires"):
        resolve_parser_mode(parser, A, chunk_method="naive", **context)


def test_pipeline_only_skips_special_builtin_check_but_clear_checks_retained_builtin() -> None:
    # Historical wrong builtin may select a pipeline, but cannot become PPT naive.
    assert resolve_parser_mode("naive", "", pipeline_id=A, filename="legacy.PPTX").parser_id == "naive"
    with pytest.raises(ValueError, match="presentation"):
        resolve_parser_mode("naive", A, pipeline_id="", filename="legacy.PPTX")
    assert resolve_parser_mode("naive", A, chunk_method="presentation", pipeline_id="", filename="legacy.PPTX").parser_id == "presentation"


def test_explicit_unset_and_frozen_result() -> None:
    plan = resolve_parser_mode("audio", A, chunk_method=UNSET, pipeline_id=UNSET, file_type="aural")
    assert plan == ParserModePlan("pipeline", "audio", A, False)
    with pytest.raises(FrozenInstanceError):
        plan.reset_needed = True  # type: ignore[misc]


@pytest.mark.parametrize("builtin", [None, "", "unknown"])
def test_current_parser_never_silently_becomes_none(builtin: Any) -> None:
    with pytest.raises(ValueError):
        resolve_parser_mode(builtin, A, pipeline_id=B)
