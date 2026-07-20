import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_tenant_utils(monkeypatch, calls, *, responses=None, tags="CHAT"):
    responses = responses or {}

    class _TenantLLMService:
        @staticmethod
        def get_api_key(db, tenant_id, model_name, model_type=None):
            calls.append((db, tenant_id, model_name, model_type))
            model_type_value = model_type.value if hasattr(model_type, "value") else model_type
            if model_type_value in responses:
                return responses[model_type_value]
            return types.SimpleNamespace(id=f"{model_type.value}:{model_name}")

    class _LLMService:
        @staticmethod
        def query(db, **kwargs):
            return [types.SimpleNamespace(tags=tags)]

    fake_api = types.ModuleType("api")
    fake_api_db = types.ModuleType("api.db")
    fake_api_db_services = types.ModuleType("api.db.services")
    fake_llm_service = types.ModuleType("api.db.services.llm_service")
    fake_llm_service.LLMService = _LLMService
    fake_tenant_llm_service = types.ModuleType("api.db.services.tenant_llm_service")
    fake_tenant_llm_service.TenantLLMService = _TenantLLMService

    for name, module in {
        "api": fake_api,
        "api.db": fake_api_db,
        "api.db.services": fake_api_db_services,
        "api.db.services.llm_service": fake_llm_service,
        "api.db.services.tenant_llm_service": fake_tenant_llm_service,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "tenant_utils_subject",
        ROOT / "api/utils/tenant_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ensure_tenant_model_id_passes_expected_model_type(monkeypatch):
    calls = []
    module = _load_tenant_utils(monkeypatch, calls)

    params = module.ensure_tenant_model_id_for_params(
        db="db",
        tenant_id="tenant-1",
        param_dict={
            "llm_id": "same-name",
            "embd_id": "same-name",
            "tenant_embd_id": "existing",
            "rerank_id": "",
        },
    )

    assert params["tenant_llm_id"] == "chat:same-name"
    assert params["tenant_embd_id"] == "existing"
    assert params["tenant_rerank_id"] is None
    assert calls == [("db", "tenant-1", "same-name", module.LLMType.CHAT)]


def test_ensure_tenant_model_id_uses_capable_multimodal_chat_fallback(monkeypatch):
    calls = []
    fallback = types.SimpleNamespace(id=7, llm_name="gemini-2.5-flash", llm_factory="Gemini")
    module = _load_tenant_utils(monkeypatch, calls, responses={"chat": None, None: fallback})

    params = module.ensure_tenant_model_id_for_params(
        db="db",
        tenant_id="tenant-1",
        param_dict={"llm_id": "gemini-2.5-flash@Gemini"},
        strict=True,
    )

    assert params["tenant_llm_id"] == 7
    assert calls == [
        ("db", "tenant-1", "gemini-2.5-flash@Gemini", module.LLMType.CHAT),
        ("db", "tenant-1", "gemini-2.5-flash@Gemini", None),
    ]


def test_ensure_tenant_model_id_strictly_rejects_incapable_fallback(monkeypatch):
    calls = []
    fallback = types.SimpleNamespace(id=7, llm_name="vision-only", llm_factory="Example")
    module = _load_tenant_utils(
        monkeypatch,
        calls,
        responses={"chat": None, None: fallback},
        tags="IMAGE2TEXT",
    )

    with pytest.raises(module.ArgumentException, match=r"vision-only@Example.*chat"):
        module.ensure_tenant_model_id_for_params(
            db="db",
            tenant_id="tenant-1",
            param_dict={"llm_id": "vision-only@Example"},
            strict=True,
        )
