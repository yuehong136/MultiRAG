"""History references retain source types without inferring images from IDs."""

from copy import deepcopy
from typing import Any

import pytest

from api.apps.restful_apis.agent_api import _normalize_agent_session


@pytest.mark.parametrize("field", ["doc_type", "doc_type_kwd"])
@pytest.mark.parametrize("value", ["image", "table", "text", "unknown", "", None])
def test_history_retains_only_actual_source_type(field: str, value: Any) -> None:
    chunk = {
        "chunk_id": "chunk",
        "content_with_weight": "caption",
        "doc_id": "document",
        "docnm_kwd": "source",
        "kb_id": "dataset",
        "img_id": "dataset-same-image.png",
        "position_int": [[1, 2, 3, 4, 5]],
        field: value,
        "private_metadata": "never exposed",
    }
    conversation = {
        "dialog_id": "agent",
        "message": [{"role": "assistant", "content": "prologue"}, {"role": "user", "content": "query", "prompt": "secret"}, {"role": "assistant", "content": "answer", "prompt": "secret"}],
        "reference": [{"chunks": [None, chunk, []]}],
    }
    result = _normalize_agent_session(deepcopy(conversation))
    assert result["agent_id"] == "agent" and "dialog_id" not in result and "reference" not in result
    assert all("prompt" not in message for message in result["messages"])
    assert all("reference" not in message for message in result["messages"][:2])
    assert result["messages"][2]["reference"] == [
        {
            "id": "chunk",
            "content": "caption",
            "document_id": "document",
            "document_name": "source",
            "dataset_id": "dataset",
            "image_id": "dataset-same-image.png",
            "positions": [[1, 2, 3, 4, 5]],
            "citation_id": "1",
            field: value,
        }
    ]


@pytest.mark.parametrize("shape", ["array", "numeric_map", "single"])
def test_history_reference_shapes_and_assistant_pairing(shape: str) -> None:
    chunks = [{"id": "first", "doc_type": "text", "doc_type_kwd": "image", "image_id": "same"}, {"id": "second", "image_id": "same"}]
    packages = [{"chunks": [chunk]} for chunk in chunks]
    reference: Any = packages if shape == "array" else {"10": packages[1], "2": packages[0]} if shape == "numeric_map" else packages[0]
    result = _normalize_agent_session({"message": [{"role": "assistant"}, {"role": "user"}, {"role": "assistant"}, {"role": "user"}, {"role": "assistant"}], "reference": reference})
    first = result["messages"][2]["reference"][0]
    assert first["id"] == "first" and first["doc_type"] == "text" and first["doc_type_kwd"] == "image"
    if shape == "single":
        assert "reference" not in result["messages"][4]
    else:
        second = result["messages"][4]["reference"][0]
        assert second["id"] == "second" and second["image_id"] == "same" and "doc_type" not in second and "doc_type_kwd" not in second


@pytest.mark.parametrize("conversation", [{}, {"message": [], "reference": []}, {"messages": [{"role": "assistant", "content": "new"}], "agent_id": "agent", "reference": []}])
def test_empty_and_create_history_remain_compatible(conversation: dict[str, Any]) -> None:
    result = _normalize_agent_session(deepcopy(conversation))
    assert result["messages"] == conversation.get("message", conversation.get("messages", []))
    assert result["agent_id"] == conversation.get("agent_id")


@pytest.mark.parametrize("reference", [[None], [{"reference": [{"id": "legacy"}]}], [{"chunks": None}], None])
def test_runtime_history_normalizes_legacy_and_empty_references_without_mutation(reference: Any) -> None:
    conversation = {"message": [{"role": "user", "prompt": "private"}, {"role": "assistant", "content": "answer"}], "reference": reference}
    original = deepcopy(conversation)
    result = _normalize_agent_session(conversation)
    assert conversation == original
    assert "prompt" not in result["messages"][0]
    if reference and isinstance(reference[0], dict) and "reference" in reference[0]:
        assert result["messages"][1]["reference"][0]["id"] == "legacy"
    else:
        assert result["messages"][1].get("reference", []) == []


@pytest.mark.parametrize("keyed", [False, True])
def test_legacy_chunk_ids_survive_history_without_inventing_document_access(keyed: bool) -> None:
    values: list[Any] = ["legacy-chunk", 0, 9007199254740993, None, False, 1.5, "", "  ", [], ["garbage"]]
    chunks = {str(index + 10): value for index, value in enumerate(values)} if keyed else values
    conversation = {"message": [{"role": "user", "prompt": "private"}, {"role": "assistant"}], "reference": [{"chunks": chunks}]}
    original = deepcopy(conversation)
    result = _normalize_agent_session(conversation)
    assert conversation == original
    actual = result["messages"][1]["reference"]
    assert actual == [
        {
            "id": value,
            "content": str(value),
            "document_id": None,
            "document_name": None,
            "dataset_id": None,
            "image_id": None,
            "positions": None,
            **({"citation_id": str(index + 10)} if keyed else {}),
        }
        for index, value in enumerate(values[:3])
    ]
    assert "prompt" not in result["messages"][0]


def test_chunk_mapping_preserves_explicit_zero_empty_and_null_values() -> None:
    chunk = {"chunk_id": 0, "id": "fallback", "content_with_weight": "", "content": "fallback", "doc_id": None, "document_id": "fallback", "positions": [], "position_int": [[1]], "doc_type": ""}
    result = _normalize_agent_session({"message": [{"role": "user"}, {"role": "assistant"}], "reference": [{"chunks": {"0": chunk}}]})
    actual = result["messages"][1]["reference"][0]
    assert actual["id"] == 0 and actual["content"] == "" and actual["document_id"] is None and actual["positions"] == []
    assert actual["citation_id"] == "0" and actual["doc_type"] == ""


def test_filtered_history_chunks_keep_original_citation_numbers() -> None:
    result = _normalize_agent_session({"message": [{"role": "user"}, {"role": "assistant", "content": "[ID:1] [ID:3]"}], "reference": [{"chunks": [None, "legacy", False, 0]}]})
    actual = result["messages"][1]["reference"]
    assert [(chunk["citation_id"], chunk["id"]) for chunk in actual] == [("1", "legacy"), ("3", 0)]
