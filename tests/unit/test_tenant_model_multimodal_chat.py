import pytest
from sqlalchemy.orm import Session

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.joint_services import tenant_model_service


@pytest.mark.parametrize(("mask", "capability", "accepted"), [(1, "chat", True), (8, "chat", False), (1, "image2text", False), (8, "image2text", True), (9, "image2text", True), (9, "chat", True)])
def test_lookup_requires_declared_registered_capability(monkeypatch: pytest.MonkeyPatch, db: Session, mask: int, capability: str, accepted: bool) -> None:
    identifier = "a" * 32
    model = TenantModel(id=identifier, model_name="multi-model", model_type=mask, status="active", extra={})
    instance = TenantModelInstance(id="b" * 32, instance_name="private", status="active", api_key="", base_url="https://fixture.invalid", region="")
    provider = TenantModelProvider(provider_name="OpenAI", tenant_id="owner", status="active")
    monkeypatch.setattr(tenant_model_service, "model_for_id", lambda _db, key, tenant, **kwargs: (model, instance, provider))
    if accepted:
        config = tenant_model_service.get_model_config_by_type_and_name(db, "owner", capability, identifier)
        assert (config["id"], config["mdl_type"], config["instance_name"]) == (identifier, capability, "private")
    else:
        with pytest.raises(LookupError, match="requested capability"):
            tenant_model_service.get_model_config_by_type_and_name(db, "owner", capability, identifier)
    assert model.model_type == mask


@pytest.mark.parametrize("capabilities,expected", [(["embedding", "image2text"], "image2text"), (["image2text", "chat"], "chat"), (["rerank", "chat"], "chat")])
def test_dialog_selects_chat_capability_independent_of_order(capabilities: list[str], expected: str) -> None:
    from api.db.services.dialog_service import _chat_capability

    assert _chat_capability(capabilities) == expected


@pytest.mark.parametrize("capabilities", [[], ["ocr"], ["embedding", "rerank"]])
def test_dialog_rejects_non_chat_capabilities(capabilities: list[str]) -> None:
    from api.db.services.dialog_service import _chat_capability

    with pytest.raises(LookupError, match="chat or vision"):
        _chat_capability(capabilities)
