import json
from typing import Any

import boto3
import litellm
import pytest
from botocore.stub import Stubber
from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from common.constants import LLMType
from core.llm import CvModel
from core.llm.cv import BedrockCV


@pytest.mark.parametrize("envelope", [False, True])
def test_tenant_factory_bedrock_image_and_tokens(monkeypatch: pytest.MonkeyPatch, db: Session, envelope: bool) -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> litellm.ModelResponse:
        calls.append(kwargs)
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": " caption "}}], usage={"total_tokens": 12})

    monkeypatch.setattr(litellm, "completion", completion)
    key = json.dumps({"auth_mode": "access_key_secret", "bedrock_ak": "test-ak", "bedrock_sk": "test-sk", "bedrock_region": "eu-west-1"})
    if envelope:
        key = json.dumps({"api_key": key, "is_tools": False})
    config = {"llm_factory": "Bedrock", "llm_name": "amazon.nova-pro-v1:0", "mdl_type": LLMType.IMAGE2TEXT.value, "api_key": key, "api_base": ""}
    model = TenantLLMService.model_instance(db, "test-tenant", config, "English")
    assert type(model) is CvModel["Bedrock"] is BedrockCV
    assert model.lang == "English"
    assert model.describe_with_prompt(b"\xff\xd8image", "read this") == ("caption", 12)
    assert model.describe(b"image") == ("caption", 12)
    assert calls[0]["model"] == "bedrock/amazon.nova-pro-v1:0"
    assert calls[0]["aws_region_name"] == "eu-west-1"
    assert calls[0]["aws_access_key_id"] == "test-ak"
    assert calls[0]["aws_secret_access_key"] == "test-sk"
    assert calls[0]["timeout"] == 60
    content = calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "read this"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_bedrock_role_uses_real_sts_contract_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = boto3.client("sts", region_name="eu-west-1", aws_access_key_id="source-ak", aws_secret_access_key="source-sk")
    with Stubber(client) as stub:
        stub.add_response(
            "assume_role",
            {"Credentials": {"AccessKeyId": "A" * 16, "SecretAccessKey": "secret", "SessionToken": "token", "Expiration": "2030-01-01T00:00:00Z"}},
            {"RoleArn": "arn:aws:iam::123456789012:role/test", "RoleSessionName": "BedrockCVSession"},
        )
        monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
        model = BedrockCV(json.dumps({"auth_mode": "iam_role", "aws_role_arn": "arn:aws:iam::123456789012:role/test", "bedrock_region": "eu-west-1"}), "m")
        assert model._get_aws_creds() == {"aws_region_name": "eu-west-1", "aws_access_key_id": "A" * 16, "aws_secret_access_key": "secret", "aws_session_token": "token"}
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("region", [None, ""])
@pytest.mark.parametrize("auth_mode", ["", "assume_role"])
def test_bedrock_default_credential_chain(region: str | None, auth_mode: str) -> None:
    assert BedrockCV(json.dumps({"auth_mode": auth_mode, "bedrock_region": region}), "m")._get_aws_creds() == {"aws_region_name": "us-east-1"}


@pytest.mark.parametrize("key", ["sensitive-not-json", "[]", '{"auth_mode":"access_key_secret"}', '{"auth_mode":"iam_role"}', '{"auth_mode":"typo"}'])
def test_bedrock_invalid_credentials_do_not_expose_key(key: str) -> None:
    with pytest.raises(ValueError) as exc:
        BedrockCV(key, "m")
    assert key not in str(exc.value)


@pytest.mark.parametrize("choices", [[], [{"message": {"role": "assistant", "content": None}}]])
def test_bedrock_empty_response_fails(monkeypatch: pytest.MonkeyPatch, choices: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: litellm.ModelResponse(choices=choices))
    with pytest.raises(ValueError, match="empty response"):
        BedrockCV("{}", "m").describe(b"image")
