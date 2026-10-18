import pytest
from sqlalchemy.orm import Session

from api.db.services import tenant_llm_service
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from common.constants import LLMType


class _RecordingModel:
    def __init__(
        self,
        api_key: str,
        model_name: str,
        *,
        base_url: str,
        **kwargs: object,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = base_url
        self.kwargs = kwargs


@pytest.mark.parametrize(
    ("model_type", "registry_name"),
    [
        (LLMType.CHAT.value, "ChatModel"),
        (LLMType.EMBEDDING.value, "EmbeddingModel"),
    ],
)
def test_model_instance_uses_model_specific_api_base(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    model_type: str,
    registry_name: str,
) -> None:
    monkeypatch.setattr(
        tenant_llm_service,
        registry_name,
        {"RegionalProvider": _RecordingModel},
    )
    model_config = {
        "llm_factory": "RegionalProvider",
        "llm_name": "regional-model",
        "mdl_type": model_type,
        "api_key": "test-key",
        "api_base": "https://regional.example/v1",
    }

    model = TenantLLMService.model_instance(db, "tenant-1", model_config)

    assert isinstance(model, _RecordingModel)
    assert model.base_url == "https://regional.example/v1"


@pytest.mark.parametrize(
    "type_fields",
    [
        {"mdl_type": LLMType.CHAT.value},
        {"model_type": LLMType.CHAT.value},
        {"mdl_type": LLMType.CHAT.value, "model_type": LLMType.RERANK.value},
    ],
)
def test_bundle_missing_provider_reports_resolved_model_type_without_credentials(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    type_fields: dict[str, str],
) -> None:
    monkeypatch.setattr(tenant_llm_service, "ChatModel", {})
    config = {
        **type_fields,
        "llm_factory": "UnavailableProvider",
        "llm_name": "missing-model",
        "api_key": "credential-marker",
        "api_base": "https://endpoint-marker.example/v1?token=credential-marker",
    }

    with pytest.raises(AssertionError) as error:
        LLMBundle(db, "tenant-1", config)

    assert str(error.value) == "Can't find model for tenant-1/chat/missing-model"
    for sensitive in (config["api_key"], config["api_base"]):
        assert sensitive not in str(error.value)
        assert sensitive not in caplog.text
