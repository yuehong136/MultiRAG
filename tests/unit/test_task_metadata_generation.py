from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy.orm import Session

from common.metadata_utils import build_metadata_config, turn2jsonschema, update_metadata_to
from core.prompts.generator import gen_metadata
from core.svr import task_executor


class MetadataModel:
    llm_name = "metadata-test"
    max_length = 8192

    def __init__(self, answer: str = '{"author": "Ada"}') -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def async_chat(self, system: str, messages: list[dict[str, Any]]) -> str:
        self.prompts.append(system)
        return self.answer


def test_upgrade_schema_keeps_constraints_and_merges_builtin_properties() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"author": {"type": "string", "enum": ["Ada"]}, "year": {"type": "integer", "minimum": 2000}},
        "required": ["author", "year"],
        "additionalProperties": False,
        "allOf": [{"properties": {"year": {"maximum": 2030}}}],
    }
    config = {"metadata": schema, "built_in_metadata": [{"key": "source", "description": "Source"}]}
    original = deepcopy(config)
    merged = build_metadata_config(config)
    assert isinstance(merged, dict)
    assert merged == {**schema, "properties": {**schema["properties"], "source": {"description": "Source"}}}
    merged["properties"]["author"]["enum"].append("Bob")
    assert config == original


@pytest.mark.parametrize("properties", [None, [], "invalid"])
def test_invalid_schema_properties_become_empty_object(properties: Any) -> None:
    assert build_metadata_config({"metadata": {"type": "object", "properties": properties, "required": ["author"]}}) == {"type": "object", "properties": {}}
    assert build_metadata_config({"metadata": {"properties": properties}, "built_in_metadata": [{"key": "source"}]}) == {"type": "object", "properties": {"source": {"description": ""}}}


@pytest.mark.parametrize("metadata", [None, "old value", 7, False])
def test_non_schema_config_uses_builtin_fields(metadata: Any) -> None:
    builtin = [{"key": "source", "description": None, "enum": None}]
    merged = build_metadata_config({"metadata": metadata, "built_in_metadata": builtin})
    assert merged == builtin
    assert turn2jsonschema(merged) == {"type": "object", "properties": {"source": {"description": ""}}, "additionalProperties": False}


def test_legacy_fields_append_builtins_and_builtin_collision_wins() -> None:
    config = {"metadata": [{"key": "author", "descriptions": "Author"}], "built_in_metadata": [{"key": "author", "enum": ["Ada"]}]}
    assert build_metadata_config(config) == config["metadata"] + config["built_in_metadata"]
    assert turn2jsonschema(build_metadata_config(config))["properties"]["author"] == {"description": "", "enum": ["Ada"], "type": "string"}
    config["metadata"] = {"type": "object", "properties": {"author": {"description": "Old"}}, "required": ["author"]}
    merged = build_metadata_config(config)
    assert merged["required"] == ["author"]
    assert merged["properties"]["author"]["enum"] == ["Ada"]


async def test_prompt_preserves_schema_and_handles_enum_without_description() -> None:
    schema = {"type": "object", "properties": {"author": {"type": "string", "enum": ["Ada"]}, "empty": {"enum": []}, "allowed": True}, "required": ["author"], "additionalProperties": False}
    original = deepcopy(schema)
    model = MetadataModel()
    assert await gen_metadata(model, schema, "Author: Ada") == model.answer
    assert await gen_metadata(model, schema, "Author: Ada") == model.answer
    assert schema == original
    assert model.prompts[0] == model.prompts[1]
    assert "required" in model.prompts[0] and "additionalProperties" in model.prompts[0]


@pytest.mark.parametrize("schema", [{}, {"properties": None}, {"type": "object", "properties": {}}])
async def test_empty_or_invalid_schema_does_not_call_model(schema: dict[str, Any]) -> None:
    model = MetadataModel()
    assert await gen_metadata(model, schema, "content") == ""
    assert model.prompts == []


def test_metadata_merge_preserves_json_values_and_existing_string_semantics() -> None:
    existing = {"outline": [{"title": "Chapter", "depth": 0}], "_isCurrent": True, "_version": 0, "score": 0.5, "missing": None, "object": {"nested": [1]}, "empty": []}
    generated = {"author": "Ada", "tags": ["generated", "shared"], "year": 2026}
    assert update_metadata_to({}, generated) == generated
    assert update_metadata_to(deepcopy(generated), {**existing, "author": "Manual", "tags": ["shared", "existing"]}) == {
        **generated,
        **existing,
        "author": "Manual",
        "tags": ["generated", "shared", "existing"],
    }
    assert update_metadata_to({}, '{"year": 2026, "active": false, "missing": null}') == {"year": 2026, "active": False, "missing": None}
    assert update_metadata_to({"outline": existing["outline"]}, {"outline": ["bad"]}) == {"outline": existing["outline"]}
    assert update_metadata_to({}, '["not an object"]') == {}


@pytest.fixture
def metadata_task(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], MetadataModel, list[dict[str, Any]], list[tuple[str, Any]], list[str]]:
    model = MetadataModel('{"author": "Ada", "tags": ["generated"], "year": 2026}')
    saved: list[dict[str, Any]] = []
    cache: dict[str, str] = {}
    cache_calls: list[tuple[str, Any]] = []
    progress: list[str] = []

    async def get_binary(bucket: str, name: str) -> bytes:
        return b"Author: Ada"

    async def chunk(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"content_with_weight": "Author: Ada"}]

    def get_cache(model: str, content: str, history: str, config: Any) -> str | None:
        cache_calls.append(("get", deepcopy(config)))
        return cache.get(repr(config))

    def set_cache(model: str, content: str, value: str, history: str, config: Any) -> None:
        cache_calls.append(("set", deepcopy(config)))
        cache[repr(config)] = value

    def save(db: Session, doc_id: str, metadata: dict[str, Any]) -> bool:
        saved.append(deepcopy(metadata))
        return True

    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", lambda *args, **kwargs: ("scratch", "metadata.txt"))
    monkeypatch.setattr(task_executor, "get_storage_binary", get_binary)
    monkeypatch.setattr(task_executor, "thread_pool_exec", chunk)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: model)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "get_llm_cache", get_cache)
    monkeypatch.setattr(task_executor, "set_llm_cache", set_cache)
    monkeypatch.setattr(task_executor.DocMetadataService, "get_document_metadata", lambda *args: {"owner": "Manual", "_version": 0, "outline": [{"title": "Chapter", "depth": 0}]})
    monkeypatch.setattr(task_executor.DocMetadataService, "update_document_metadata", save)
    task = {
        "id": "task",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "parser_id": "naive",
        "name": "metadata.txt",
        "location": "metadata.txt",
        "size": 11,
        "from_page": 0,
        "to_page": 1,
        "language": "English",
        "llm_id": "fake",
        "parser_config": {},
        "kb_parser_config": {},
    }
    return task, model, saved, cache_calls, progress


async def test_task_cache_uses_merged_config_for_lookup_and_write(db: Session, metadata_task: Any) -> None:
    task, model, saved, cache_calls, progress = metadata_task
    config = {"enable_metadata": True, "metadata": {"type": "object", "properties": {"author": {"enum": ["Ada"]}}, "required": ["author"]}, "built_in_metadata": [{"key": "source"}]}
    task["parser_config"] = deepcopy(config)
    for _ in range(2):
        docs = await task_executor.build_chunks(task, lambda *args, **kwargs: progress.append(kwargs.get("msg", "")), db)
        assert docs and all("metadata_obj" not in doc for doc in docs)
    assert len(model.prompts) == 1
    assert cache_calls == [("get", build_metadata_config(config)), ("set", build_metadata_config(config)), ("get", build_metadata_config(config))]
    assert task["parser_config"] == config
    assert saved[0]["_version"] == 0 and saved[0]["year"] == 2026
    assert saved[0]["outline"] == [{"title": "Chapter", "depth": 0}]
    task["parser_config"]["built_in_metadata"] = [{"key": "source", "enum": ["New"]}]
    await task_executor.build_chunks(task, lambda *args, **kwargs: None, db)
    assert len(model.prompts) == 2


@pytest.mark.parametrize("metadata", [[], {}, {"properties": None}, "invalid"])
async def test_empty_task_metadata_never_crashes_or_writes(db: Session, metadata_task: Any, metadata: Any) -> None:
    task, model, saved, cache_calls, progress = metadata_task
    task["parser_config"] = {"enable_metadata": True, "metadata": metadata}
    docs = await task_executor.build_chunks(task, lambda *args, **kwargs: None, db)
    assert docs and all("metadata_obj" not in doc for doc in docs)
    assert model.prompts == [] and saved == [] and cache_calls == []


async def test_disabled_metadata_does_not_generate_or_write(db: Session, metadata_task: Any) -> None:
    task, model, saved, cache_calls, progress = metadata_task
    task["parser_config"] = {"enable_metadata": False, "metadata": [{"key": "author"}], "built_in_metadata": [{"key": "source"}]}
    assert await task_executor.build_chunks(task, lambda *args, **kwargs: None, db)
    assert model.prompts == [] and saved == [] and cache_calls == []


async def test_task_empty_model_reply_never_crashes_or_writes(db: Session, metadata_task: Any) -> None:
    task, model, saved, cache_calls, progress = metadata_task
    task["parser_config"] = {"enable_metadata": True, "metadata": [{"key": "author"}]}
    model.answer = ""
    assert await task_executor.build_chunks(task, lambda *args, **kwargs: None, db)
    assert saved == []
    assert all(operation == "get" for operation, _ in cache_calls)


async def test_task_failed_metadata_write_does_not_report_completion(db: Session, monkeypatch: pytest.MonkeyPatch, metadata_task: Any) -> None:
    task, model, saved, cache_calls, progress = metadata_task
    task["parser_config"] = {"enable_metadata": True, "built_in_metadata": [{"key": "source"}]}
    monkeypatch.setattr(task_executor.DocMetadataService, "update_document_metadata", lambda *args: False)
    with pytest.raises(RuntimeError, match="Failed to persist generated metadata"):
        await task_executor.build_chunks(task, lambda *args, **kwargs: progress.append(kwargs.get("msg", "")), db)
    assert not any("Metadata generation" in message for message in progress)
