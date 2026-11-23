from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.utils import tenant_utils
from common.constants import LLMType


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[Session, str, str, LLMType | None]],
    *,
    responses: dict[str | None, SimpleNamespace | None] | None = None,
    tags: str = "CHAT",
) -> None:
    def get_api_key(_cls: type, db: Session, tenant_id: str, model_name: str, model_type: LLMType | None = None) -> SimpleNamespace | None:
        calls.append((db, tenant_id, model_name, model_type))
        value = model_type.value if model_type is not None else None
        if responses is not None and value in responses:
            return responses[value]
        return SimpleNamespace(id=f"{value}:{model_name}")

    def query(_cls: type, db: Session, **filters: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(tags=tags)]

    monkeypatch.setattr(tenant_utils.TenantLLMService, "get_api_key", classmethod(get_api_key))
    monkeypatch.setattr(tenant_utils.LLMService, "query", classmethod(query))


def test_ensure_tenant_model_id_passes_expected_model_type(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    calls: list[tuple[Session, str, str, LLMType | None]] = []
    _patch_lookup(monkeypatch, calls)
    params = tenant_utils.ensure_tenant_model_id_for_params(
        db=db,
        tenant_id="tenant-1",
        param_dict={"llm_id": "same-name", "embd_id": "same-name", "tenant_embd_id": "existing", "rerank_id": ""},
    )
    assert params["tenant_llm_id"] == "chat:same-name"
    assert params["tenant_embd_id"] == "existing"
    assert params["tenant_rerank_id"] is None
    assert calls == [(db, "tenant-1", "same-name", LLMType.CHAT)]


def test_ensure_tenant_model_id_uses_capable_multimodal_chat_fallback(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    calls: list[tuple[Session, str, str, LLMType | None]] = []
    fallback = SimpleNamespace(id=7, llm_name="gemini-2.5-flash", llm_factory="Gemini")
    _patch_lookup(monkeypatch, calls, responses={"chat": None, None: fallback})
    params = tenant_utils.ensure_tenant_model_id_for_params(db, "tenant-1", {"llm_id": "gemini-2.5-flash@Gemini"}, strict=True)
    assert params["tenant_llm_id"] == 7
    assert calls == [(db, "tenant-1", "gemini-2.5-flash@Gemini", LLMType.CHAT), (db, "tenant-1", "gemini-2.5-flash@Gemini", None)]


def test_ensure_tenant_model_id_strictly_rejects_incapable_fallback(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    calls: list[tuple[Session, str, str, LLMType | None]] = []
    fallback = SimpleNamespace(id=7, llm_name="vision-only", llm_factory="Example")
    _patch_lookup(monkeypatch, calls, responses={"chat": None, None: fallback}, tags="IMAGE2TEXT")
    with pytest.raises(tenant_utils.ArgumentException, match=r"vision-only@Example.*chat"):
        tenant_utils.ensure_tenant_model_id_for_params(db, "tenant-1", {"llm_id": "vision-only@Example"}, strict=True)


@pytest.mark.parametrize("declared,expected", [("image2text", LLMType.IMAGE2TEXT), ("chat", LLMType.CHAT), ("embedding", LLMType.CHAT), (None, LLMType.CHAT), (["image2text"], LLMType.CHAT)])
def test_chat_reference_keeps_declared_capability_without_changing_other_roles(monkeypatch: pytest.MonkeyPatch, db: Session, declared: Any, expected: LLMType) -> None:
    calls: list[tuple[Session, str, str, LLMType | None]] = []
    _patch_lookup(monkeypatch, calls)
    params = tenant_utils.ensure_tenant_model_id_for_params(db, "tenant-1", {"llm_id": "17", "llm_setting": {"model_type": declared}, "embd_id": "18", "rerank_id": "19"}, strict=True)
    assert params["tenant_llm_id"] == f"{expected.value}:17"
    assert params["tenant_embd_id"] == "embedding:18" and params["tenant_rerank_id"] == "rerank:19"
    assert calls == [(db, "tenant-1", "17", expected), (db, "tenant-1", "18", LLMType.EMBEDDING), (db, "tenant-1", "19", LLMType.RERANK)]


def test_strict_vision_reference_rejects_unavailable_model_without_chat_fallback(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    calls: list[tuple[Session, str, str, LLMType | None]] = []
    _patch_lookup(monkeypatch, calls, responses={"image2text": None, "chat": SimpleNamespace(id=99)})
    with pytest.raises(tenant_utils.ArgumentException, match=r"17.*image2text"):
        tenant_utils.ensure_tenant_model_id_for_params(db, "foreign-tenant", {"llm_id": "17", "llm_setting": {"model_type": "image2text"}}, strict=True)
    assert calls == [(db, "foreign-tenant", "17", LLMType.IMAGE2TEXT)]
