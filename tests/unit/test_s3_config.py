"""S3 configuration uses the same region field from loading through boto3."""

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from common import app_config, settings
from common.app_config import AppConfigError, load_app_config
from core.utils import s3_conn


def test_s3_loaded_config_reaches_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    raw: dict[str, Any] = {
        "s3": {
            "access_key": "test-access",
            "secret_key": "test-secret",
            "region_name": "base-region",
            "endpoint_url": "https://storage.example.test",
            "bucket": "physical",
            "prefix_path": "environment",
            "signature_version": "v4",
            "addressing_style": "virtual",
            "session_token": "test-session",
            "future_field": {"enabled": True},
        }
    }
    monkeypatch.setattr(app_config, "read_config", lambda _name: raw)
    monkeypatch.setenv("MULTIRAG_S3__REGION_NAME", "auto")
    monkeypatch.setenv("STORAGE_IMPL", "AWS_S3")
    config = load_app_config()
    assert config.s3.region_name == "auto"
    assert config.s3.model_dump()["future_field"] == {"enabled": True}
    assert config.get_section("s3")["session_token"] == "test-session"
    monkeypatch.setattr(settings, "get_app_config", lambda: config)
    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(s3_conn.boto3, "client", factory)
    adapter_class = inspect.getclosurevars(s3_conn.MultiRAGS3).nonlocals["cls"]
    adapter = adapter_class()
    args, kwargs = factory.call_args
    assert args == ("s3",)
    assert kwargs["region_name"] == "auto"
    assert kwargs["endpoint_url"] == "https://storage.example.test"
    assert kwargs["aws_session_token"] == "test-session"
    assert kwargs["config"].signature_version == "v4"
    assert kwargs["config"].s3 == {"addressing_style": "virtual"}
    assert "future_field" not in kwargs
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b"data")}
    assert adapter.get_bytes("logical", "file") == b"data"
    client.get_object.assert_called_once_with(Bucket="physical", Key="environment/logical/file")


@pytest.mark.parametrize("value", [123, [], {}])
def test_s3_region_type_fails_at_load(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    monkeypatch.setattr(app_config, "read_config", lambda _name: {"s3": {"region_name": value}})
    monkeypatch.delenv("MULTIRAG_S3__REGION_NAME", raising=False)
    with pytest.raises(AppConfigError, match=r"s3\.region_name"):
        load_app_config()


def test_docker_template_keeps_s3_region_environment_contract() -> None:
    template = Path(__file__).resolve().parents[2] / "docker/service_conf.yaml.template"
    sections = yaml.safe_load(template.read_text())
    assert sections["s3"]["region_name"] == "${S3_REGION:-region}"
    assert "region" not in sections["s3"]
    assert sections["oss"]["region"] == "${OSS_REGION:-cn-hangzhou}"
