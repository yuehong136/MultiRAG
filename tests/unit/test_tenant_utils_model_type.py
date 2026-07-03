import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_tenant_utils(monkeypatch, calls):
    class _TenantLLMService:
        @staticmethod
        def get_api_key(db, tenant_id, model_name, model_type=None):
            calls.append((db, tenant_id, model_name, model_type))
            return types.SimpleNamespace(id=f"{model_type.value}:{model_name}")

    fake_api = types.ModuleType("api")
    fake_api_db = types.ModuleType("api.db")
    fake_api_db_services = types.ModuleType("api.db.services")
    fake_tenant_llm_service = types.ModuleType("api.db.services.tenant_llm_service")
    fake_tenant_llm_service.TenantLLMService = _TenantLLMService

    for name, module in {
        "api": fake_api,
        "api.db": fake_api_db,
        "api.db.services": fake_api_db_services,
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
