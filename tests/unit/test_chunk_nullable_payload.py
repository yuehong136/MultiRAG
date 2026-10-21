"""Nullable mapped fields retain the legacy omitted-field chunk contract."""

import sys
from typing import Any

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize("available,expected", [(None, True), (0, False), (1, True)])
def test_nullable_chunk_payload_preserves_defaults_and_disabled_zero(client: Any, available: int | None, expected: bool) -> None:
    module = sys.modules["api.apps.restful_apis.chunk"]
    fields = ["content_with_weight", "docnm_kwd", "important_kwd", "question_kwd", "img_id", "position_int", "tag_kwd", "tag_feas"]
    row: dict[str, Any] = dict.fromkeys(fields)
    row.update(doc_id="doc", kb_id="kb", available_int=available)

    payload = module._chunk_payload("chunk", row)

    assert payload == module.Chunk(id="chunk", document_id="doc", available=expected).model_dump()
    assert module._chunk_payload("chunk", {}) == module.Chunk(id="chunk").model_dump()


def test_non_null_malformed_chunk_fields_still_fail_validation(client: Any) -> None:
    module = sys.modules["api.apps.restful_apis.chunk"]

    with pytest.raises(ValidationError):
        module._chunk_payload("chunk", {"important_kwd": 0})
    with pytest.raises(ValueError):
        module._chunk_payload("chunk", {"available_int": "invalid"})
