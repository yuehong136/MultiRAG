"""Real installed spaCy inference and fail-closed extraction contracts."""

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from api.utils.dataset_search import SearchDatasetsRequest
from api.utils.validation_utils import GraphragConfig
from common.exceptions import TaskCanceledException
from core.graphrag.general import index
from core.graphrag.ner import graph_extractor as ner


class NoChat:
    llm_name = "no-chat"
    max_length = 8192

    async def async_chat(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("NER extraction must not call the LLM")


@pytest.mark.parametrize(
    ("language", "text", "expected"),
    [
        ("English", "Barack Obama visited Microsoft in London.", {"BARACK OBAMA", "MICROSOFT", "LONDON"}),
        ("Chinese", "张三在北京大学工作。李四在上海工作。", {"北京大学", "上海"}),
    ],
)
async def test_real_models_entities_relations_and_provenance(language: str, text: str, expected: set[str]) -> None:
    ext = ner.GraphExtractor(NoChat(), language)
    entities, relations = await ext("doc", [text])
    names = {e["entity_name"] for e in entities}
    assert expected <= names
    if language == "Chinese":
        assert "北京 大学" not in names
    assert relations
    assert all(e["description"] and e["source_id"] == ["doc"] for e in [*entities, *relations])
    assert all(e["src_id"] in names and e["tgt_id"] in names and e["weight"] > 0 for e in relations)


async def test_repeated_entity_gets_second_sentence_relationship_without_cross_sentence_edges() -> None:
    ext = ner.GraphExtractor(NoChat(), entity_types=["person", "organization", "geo"])
    entities, edges = await ext("doc", ["Barack Obama visited Microsoft. Barack Obama visited London. Apple is based in California."])
    pairs = {(e["src_id"], e["tgt_id"]): e for e in edges}
    assert ("BARACK OBAMA", "LONDON") in pairs
    assert "London" in pairs[("BARACK OBAMA", "LONDON")]["description"]
    assert ("APPLE", "MICROSOFT") not in pairs
    assert {e["entity_type"] for e in entities} <= {"PERSON", "ORGANIZATION", "GEO"}


async def test_error_cancellation_and_missing_model_do_not_return_partial_results(monkeypatch: pytest.MonkeyPatch) -> None:
    ext = ner.GraphExtractor(NoChat(), spacy_model="multirag_model_does_not_exist")
    with pytest.raises(RuntimeError, match="install the locked"):
        await ext("doc", ["Microsoft is in London."])
    ext = ner.GraphExtractor(NoChat())
    monkeypatch.setattr(ner, "has_canceled", lambda _: True)
    with pytest.raises(TaskCanceledException):
        await ext("doc", ["Microsoft"], task_id="cancel")


async def test_inference_runs_off_event_loop_and_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    ext = ner.GraphExtractor(NoChat())
    event_loop_thread = threading.get_ident()

    def inference(*_: Any) -> Any:
        assert threading.get_ident() != event_loop_thread
        raise ValueError("inference failed")

    monkeypatch.setattr(ext, "_extract_chunk", inference)
    with pytest.raises(ValueError, match="inference failed"):
        await ext("doc", ["content"])
    await asyncio.sleep(0)


def test_method_defaults_language_and_reranker_schema() -> None:
    assert GraphragConfig().method == "light"
    assert index._select_extractor({}) is index.LightKGExt
    assert index._select_extractor({"method": "general"}) is index.GeneralKGExt
    assert index._select_extractor({"method": "ner"}) is ner.GraphExtractor
    assert GraphragConfig(method="ner").method == "ner"
    with pytest.raises(ValidationError):
        GraphragConfig(method="unknown")
    with pytest.raises(ValueError, match="supports English and Chinese"):
        ner.GraphExtractor(NoChat(), "French")
    assert ner.model_for_language("zh-CN") == "zh_core_web_sm"
    for value in [42, None]:
        assert SearchDatasetsRequest(question="q", dataset_ids=["a"], tenant_rerank_id=value).tenant_rerank_id == value
    with pytest.raises(ValidationError):
        SearchDatasetsRequest(question="q", dataset_ids=["a"], tenant_rerank_id="model-name")


async def test_source_recovery_preserves_named_entity_case_and_type() -> None:
    ext = ner.GraphExtractor(NoChat(), "English", entity_types=["organization", "geo"])
    text = "I ate an apple while Apple acquired Beats in California."
    entities, relations = await ext("doc", [text])
    records = {entity["entity_name"]: entity for entity in entities}
    assert records["APPLE"]["entity_type"] == "ORGANIZATION"
    assert "BEATS" not in records  # The locked model labels Beats as NORP/category.
    assert records["CALIFORNIA"]["entity_type"] == "GEO"
    assert any({edge["src_id"], edge["tgt_id"]} == {"APPLE", "CALIFORNIA"} for edge in relations)
    assert all(edge["description"] == text and edge["source_id"] == ["doc"] for edge in relations)
