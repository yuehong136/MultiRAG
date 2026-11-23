from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from sqlalchemy.orm import Session

from api.utils import tenant_utils
from common.constants import LLMType
from common.exceptions import ArgumentException


def test_ensure_tenant_model_id_passes_expected_model_type(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id, embedding_id = "a" * 32, "b" * 32
    lookup = Mock(side_effect=[SimpleNamespace(id=chat_id), SimpleNamespace(id=embedding_id)])
    monkeypatch.setattr(tenant_utils.TenantLLMService, "get_api_key", lookup)

    params = tenant_utils.ensure_tenant_model_id_for_params(
        db,
        "tenant-1",
        {"llm_id": chat_id, "embd_id": embedding_id, "tenant_embd_id": "c" * 32, "rerank_id": ""},
        strict=True,
    )

    assert params["tenant_llm_id"] == chat_id
    assert params["tenant_embd_id"] == embedding_id
    assert params["tenant_rerank_id"] is None
    assert lookup.call_args_list == [call(db, "tenant-1", chat_id, LLMType.CHAT), call(db, "tenant-1", embedding_id, LLMType.EMBEDDING)]


def test_ensure_tenant_model_id_uses_explicit_vision_capability(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    vision_id = "d" * 32
    lookup = Mock(return_value=SimpleNamespace(id=vision_id))
    monkeypatch.setattr(tenant_utils.TenantLLMService, "get_api_key", lookup)

    params = tenant_utils.ensure_tenant_model_id_for_params(db, "tenant-1", {"llm_id": vision_id, "llm_setting": {"model_type": "image2text"}}, strict=True)

    assert params["tenant_llm_id"] == vision_id
    lookup.assert_called_once_with(db, "tenant-1", vision_id, LLMType.IMAGE2TEXT)


def test_ensure_tenant_model_id_strictly_rejects_incompatible_chat_model(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    vision_id = "d" * 32
    lookup = Mock(return_value=None)
    monkeypatch.setattr(tenant_utils.TenantLLMService, "get_api_key", lookup)

    with pytest.raises(ArgumentException, match=rf"{vision_id}.*chat"):
        tenant_utils.ensure_tenant_model_id_for_params(db, "tenant-1", {"llm_id": vision_id}, strict=True)
    lookup.assert_called_once_with(db, "tenant-1", vision_id, LLMType.CHAT)


@pytest.mark.parametrize("declared,expected", [("image2text", LLMType.IMAGE2TEXT), ("chat", LLMType.CHAT), ("embedding", LLMType.CHAT), (None, LLMType.CHAT), (["image2text"], LLMType.CHAT)])
def test_declared_chat_capability_does_not_change_other_roles(db: Session, monkeypatch: pytest.MonkeyPatch, declared: object, expected: LLMType) -> None:
    chat_id, embedding_id, rerank_id = "a" * 32, "b" * 32, "c" * 32
    lookup = Mock(side_effect=[SimpleNamespace(id=value) for value in (chat_id, embedding_id, rerank_id)])
    monkeypatch.setattr(tenant_utils.TenantLLMService, "get_api_key", lookup)
    params = tenant_utils.ensure_tenant_model_id_for_params(
        db,
        "tenant-1",
        {"llm_id": chat_id, "llm_setting": {"model_type": declared}, "embd_id": embedding_id, "rerank_id": rerank_id},
        strict=True,
    )
    assert (params["tenant_llm_id"], params["tenant_embd_id"], params["tenant_rerank_id"]) == (chat_id, embedding_id, rerank_id)
    assert lookup.call_args_list == [call(db, "tenant-1", chat_id, expected), call(db, "tenant-1", embedding_id, LLMType.EMBEDDING), call(db, "tenant-1", rerank_id, LLMType.RERANK)]
