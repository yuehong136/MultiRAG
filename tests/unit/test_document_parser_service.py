"""Save plans reuse the accepted mode/config modules without resetting on save."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, Knowledgebase, UserCanvas
from api.db.services import document_image_lock as image_lock
from api.db.services import document_parser_service as service
from api.utils.document_pipeline_validation import validate_document_pipeline
from api.utils.document_update_contract import DocumentUpdateError, DocumentUpdatePatch, LegacyDocumentParserPatch


@pytest.mark.parametrize(("acknowledged", "confirmed", "valid"), [(1, 1, True), (0, True, True), (1, False, False), (2, 1, False), (True, True, False), (1, 1.0, False), (1, None, False)])
def test_task_image_reservation_requires_confirmed_membership(monkeypatch: pytest.MonkeyPatch, acknowledged: Any, confirmed: Any, valid: bool) -> None:
    from types import SimpleNamespace

    client = SimpleNamespace(sadd=lambda *args: acknowledged, sismember=lambda *args: confirmed)
    monkeypatch.setattr(image_lock.REDIS_CONN, "REDIS", client)
    if valid:
        image_lock.reserve_task_image("trusted-current-task", ("bucket", "image"))
    else:
        with pytest.raises(RuntimeError, match="Task image reservation could not be confirmed"):
            image_lock.reserve_task_image("trusted-current-task", ("bucket", "image"))


def definition() -> dict[str, Any]:
    names = ["File", "Parser", "TokenChunker", "Tokenizer"]
    return {
        "components": {
            name: {
                "obj": {"component_name": name, "params": {"setups": {"text&code": {"suffix": ["txt"], "output_format": "text"}}} if name == "Parser" else {}},
                "upstream": names[index - 1 : index] if index else [],
                "downstream": names[index + 1 : index + 2],
            }
            for index, name in enumerate(names)
        },
        "path": [],
    }


class ReadOnlySession(Session):
    def __init__(self, file: Any = None, canvas: Any = None) -> None:
        super().__init__()
        self.file = file
        self.canvas = canvas

    def scalar(self, statement: Any) -> Any:
        entity = statement.column_descriptions[0]["entity"]
        return self.file if entity is File else self.canvas if entity is UserCanvas else None


def document(**values: Any) -> Any:
    return Document(
        id="doc",
        kb_id="kb",
        name="display.pdf",
        location="original.pdf",
        suffix="pdf",
        type="doc",
        parser_id="naive",
        pipeline_id=None,
        parser_config={"raptor": {"use_raptor": True, "future": None}, "llm_id": "retained", "unknown": [0]},
        chunk_num=2,
        token_num=0,
        progress=1,
        status="0",
        run="3",
        **values,
    )


def prepare(doc: Any, patch: dict[str, Any], db: Any = None, actor: str = "owner") -> Any:
    return service.prepare_document_update(db or ReadOnlySession(), doc, Knowledgebase(id="kb", tenant_id="owner"), DocumentUpdatePatch.model_validate(patch), actor)


@pytest.mark.parametrize(
    "patch", [{}, {"chunk_method": "naive"}, {"parser_config": {}}, {"parser_config": {"raptor": {"use_raptor": False}}}, {"enabled": False}, {"name": "new.pdf"}, {"meta_fields": {"score": 0}}]
)
def test_non_mode_save_preserves_history_and_null_pipeline(patch: dict[str, Any]) -> None:
    doc = document()
    original = copy.deepcopy({key: value for key, value in vars(doc).items() if not key.startswith("_sa_")})
    plan = prepare(doc, patch)
    assert plan.reset is False and "run" not in plan.values
    assert "pipeline_id" not in plan.values or plan.values["pipeline_id"] == ""
    if "chunk_method" not in patch:
        assert "pipeline_id" not in plan.values
    assert {key: value for key, value in vars(doc).items() if not key.startswith("_sa_")} == original
    if "parser_config" in plan.values:
        assert plan.values["parser_config"]["unknown"] == [0]
        assert plan.values["parser_config"]["raptor"]["future"] is None
        assert plan.values["parser_config"]["llm_id"] == "retained"


def test_explicit_builtin_leaves_pipeline_once_and_empty_clears() -> None:
    doc = document()
    doc.pipeline_id = "a" * 32
    for patch in [{"chunk_method": "naive"}, {"pipeline_id": ""}]:
        plan = prepare(doc, patch)
        assert plan.reset is True and plan.values["pipeline_id"] == ""


@pytest.mark.parametrize(
    "patch", [{"chunk_method": "general"}, {"pipeline_id": "invalid"}, {"chunk_method": "paper", "pipeline_id": "a" * 32}, {"chunk_count": 3}, {"token_count": 1}, {"progress": 0}]
)
def test_invalid_mixed_request_never_prepares_effects(monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any]) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid request reached side effect preparation")

    monkeypatch.setattr(service, "prepare_update_effects", forbidden)
    doc = document()
    before = copy.deepcopy({key: value for key, value in vars(doc).items() if not key.startswith("_sa_")})
    with pytest.raises(DocumentUpdateError):
        service.prepare_document_update(
            ReadOnlySession(),
            doc,
            Knowledgebase(id="kb", tenant_id="owner"),
            DocumentUpdatePatch.model_validate({"name": "new.pdf", "enabled": True, "meta_fields": {"key": "value"}, **patch}),
            "owner",
            lock=True,
        )
    assert {key: value for key, value in vars(doc).items() if not key.startswith("_sa_")} == before


@pytest.mark.parametrize("kind,location,required", [("visual", "opaque", "picture"), ("aural", "opaque", "audio"), ("doc", "original.pptx", "presentation"), ("doc", "original.eml", "email")])
def test_saved_source_classification_wins_over_display_and_request_name(kind: str, location: str, required: str) -> None:
    doc = document()
    doc.type = kind
    file = File(location=location, name="saved.pdf")
    with pytest.raises(DocumentUpdateError):
        prepare(doc, {"name": "new.pdf", "chunk_method": "naive"}, ReadOnlySession(file=file))
    plan = prepare(doc, {"chunk_method": required}, ReadOnlySession(file=file))
    assert plan.values["parser_id"] == required and plan.reset


def test_stored_legacy_parser_retained_but_invalid_parser_requires_repair() -> None:
    doc = document()
    doc.parser_id = "general"
    assert prepare(doc, {"parser_config": {}}).reset is False
    for value in [None, "invalid"]:
        doc.parser_id = value
        with pytest.raises(DocumentUpdateError):
            prepare(doc, {})


@pytest.mark.parametrize("source", ["gmail", "knowledgebase"])
def test_nonlocal_file_uses_the_actual_document_storage_address(source: str) -> None:
    doc = document()
    doc.location, doc.suffix = "actual-source.eml", "eml"
    file = File(location="origin.pdf", name="linked.pdf", source_type=source)
    with pytest.raises(DocumentUpdateError):
        prepare(doc, {"chunk_method": "naive", "name": "new.pdf"}, ReadOnlySession(file=file))
    assert prepare(doc, {"chunk_method": "email"}, ReadOnlySession(file=file)).values["parser_id"] == "email"


@pytest.mark.parametrize(
    "owner,permission,status,category,actor,allowed",
    [
        ("owner", "me", "1", "dataflow_canvas", "owner", True),
        ("owner", "team", "1", "dataflow_canvas", "admin", True),
        ("owner", "me", "1", "dataflow_canvas", "admin", False),
        ("other", "team", "1", "dataflow_canvas", "owner", False),
        ("owner", "team", "0", "dataflow_canvas", "owner", False),
        ("owner", "team", "1", "agent_canvas", "owner", False),
    ],
)
def test_pipeline_permissions_category_and_actual_parameter_validation(owner: str, permission: str, status: str, category: str, actor: str, allowed: bool) -> None:
    canvas = UserCanvas(user_id=owner, permission=permission, canvas_category=category, dsl=definition())
    if allowed:
        assert prepare(document(), {"pipeline_id": "a" * 32}, ReadOnlySession(canvas=canvas if status == "1" else None), actor).reset is True
    else:
        with pytest.raises(DocumentUpdateError):
            prepare(document(), {"pipeline_id": "a" * 32}, ReadOnlySession(canvas=canvas if status == "1" else None), actor)


def test_definition_validation_does_not_construct_or_execute_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.canvas

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Definition validation created a pool")

    monkeypatch.setattr(agent.canvas, "ThreadPoolExecutor", forbidden)
    dsl = definition()
    original = copy.deepcopy(dsl)
    validate_document_pipeline(dsl)
    assert dsl == original


@pytest.mark.parametrize("fault", ["empty", "root", "class", "params", "edge", "path", "saved_path", "missing_edge", "cycle", "reference"])
def test_invalid_canonical_definition_is_rejected(fault: str) -> None:
    dsl = definition()
    if fault == "empty":
        dsl = {}
    elif fault == "root":
        dsl["components"]["File"]["obj"]["component_name"] = "Begin"
    elif fault == "class":
        dsl["components"]["Parser"]["obj"]["component_name"] = "Unknown"
    elif fault == "params":
        dsl["components"]["TokenChunker"]["obj"]["params"] = {"chunk_token_size": 0}
    elif fault == "edge":
        dsl["components"]["File"]["downstream"] = ["missing"]
    elif fault == "path":
        dsl["path"] = ["missing"]
    elif fault == "saved_path":
        dsl["path"] = ["File", "Parser"]
    elif fault == "missing_edge":
        del dsl["components"]["Tokenizer"]["downstream"]
    elif fault == "cycle":
        dsl["components"]["Tokenizer"]["downstream"] = ["Parser"]
    else:
        dsl["components"]["Parser"]["obj"]["params"] = {"input": "{missing@text}"}
    with pytest.raises((ValueError, AssertionError)):
        validate_document_pipeline(dsl)


def test_legacy_presence_mapping_reuses_exact_document_patch() -> None:
    assert LegacyDocumentParserPatch(doc_id="doc").document_patch().model_dump(exclude_unset=True) == {}
    assert LegacyDocumentParserPatch(doc_id="doc", parser_id="naive", pipeline_id="", parser_config={}).document_patch().model_dump(exclude_unset=True) == {
        "chunk_method": "naive",
        "pipeline_id": "",
        "parser_config": {},
    }
    for fields in [{"parser_id": None}, {"pipeline_id": None}, {"parser_config": None}, {"name": "unknown"}]:
        with pytest.raises(ValidationError):
            LegacyDocumentParserPatch.model_validate({"doc_id": "doc", **fields})
