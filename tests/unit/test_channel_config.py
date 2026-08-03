"""飞书 Channel 强类型配置与配置日志脱敏测试。"""

import base64
import json
import os
import textwrap
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from common import config_utils
from common.app_config import AppConfig, AppConfigError, load_app_config, reset_app_config
from common.config_utils import _mask_sensitive_fields
from common.constants import SERVICE_CONF


@pytest.fixture
def conf_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Callable[[str, str], None], None, None]:
    """将配置读取隔离到临时目录，并在测试前后清空缓存。"""
    (tmp_path / "configs").mkdir()
    monkeypatch.setattr(config_utils, "get_project_base_directory", lambda: str(tmp_path))
    reset_app_config()

    def write(name: str, content: str) -> None:
        (tmp_path / "configs" / name).write_text(textwrap.dedent(content), encoding="utf-8")

    yield write
    reset_app_config()


def _enabled_feishu_config() -> dict[str, object]:
    return {
        "enabled": True,
        "app_id": "cli_demo",
        "app_secret": "app-secret-value",
        "domain": "feishu",
        "multirag_base_url": "http://127.0.0.1:9380",
        "agent_id": "agent_demo",
        "agent_api_token": "agent-token-value",
        "release_marker": "leadership-demo-v1",
        "allowed_open_ids": ["ou_demo"],
    }


class TestFeishuChannelConfig:
    def test_default_is_disabled_and_secrets_are_typed(self) -> None:
        feishu = AppConfig().channels.feishu

        assert feishu.enabled is False
        assert isinstance(feishu.app_secret, SecretStr)
        assert isinstance(feishu.agent_api_token, SecretStr)
        assert feishu.connect_timeout_seconds == 5
        assert feishu.total_timeout_seconds == 120
        assert feishu.leader_ttl_seconds == 30
        assert feishu.leader_renew_seconds == 10

    def test_valid_enabled_configuration(self) -> None:
        cfg = AppConfig.model_validate({"channels": {"feishu": _enabled_feishu_config()}})

        assert cfg.channels.feishu.enabled is True
        assert cfg.channels.feishu.app_secret.get_secret_value() == "app-secret-value"
        assert cfg.channels.feishu.agent_api_token.get_secret_value() == "agent-token-value"
        assert "app-secret-value" not in repr(cfg)
        assert "agent-token-value" not in repr(cfg)

    @pytest.mark.parametrize(
        "field,empty_value",
        [
            ("app_id", ""),
            ("app_secret", ""),
            ("multirag_base_url", " "),
            ("agent_id", ""),
            ("agent_api_token", ""),
            ("release_marker", ""),
        ],
    )
    def test_enabled_configuration_requires_critical_fields(self, field: str, empty_value: object) -> None:
        feishu = _enabled_feishu_config()
        feishu[field] = empty_value

        with pytest.raises(ValidationError, match=field):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    def test_domain_is_restricted(self) -> None:
        feishu = _enabled_feishu_config()
        feishu["domain"] = "example"

        with pytest.raises(ValidationError, match=r"channels\.feishu\.domain"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    def test_empty_allowlist_relies_on_feishu_app_availability(self) -> None:
        feishu = _enabled_feishu_config()
        feishu["allowed_open_ids"] = []

        cfg = AppConfig.model_validate({"channels": {"feishu": feishu}})

        assert cfg.channels.feishu.allowed_open_ids == []

    def test_nonempty_allowlist_rejects_blank_values(self) -> None:
        feishu = _enabled_feishu_config()
        feishu["allowed_open_ids"] = ["ou_demo", " "]

        with pytest.raises(ValidationError, match=r"channels\.feishu\.allowed_open_ids"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    @pytest.mark.parametrize("url", ["127.0.0.1:9380", "ftp://example.com", "http:///missing-host"])
    def test_multirag_base_url_requires_http_url_with_host(self, url: str) -> None:
        feishu = _enabled_feishu_config()
        feishu["multirag_base_url"] = url

        with pytest.raises(ValidationError, match=r"channels\.feishu\.multirag_base_url"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:9380",
            "http://localhost.:9380/",
            "http://127.0.0.1:9380",
            "http://127.255.255.254:9380",
            "http://[::1]:9380",
            "https://multirag.example.com",
            "https://10.0.0.8:9380/",
        ],
    )
    def test_multirag_base_url_accepts_local_http_or_https(self, url: str) -> None:
        feishu = _enabled_feishu_config()
        feishu["multirag_base_url"] = url

        cfg = AppConfig.model_validate({"channels": {"feishu": feishu}})

        assert cfg.channels.feishu.multirag_base_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://multirag.example.com:9380",
            "http://localhost.example.com:9380",
            "http://10.0.0.8:9380",
            "http://169.254.169.254",
        ],
    )
    def test_multirag_base_url_requires_https_for_non_loopback_hosts(self, url: str) -> None:
        feishu = _enabled_feishu_config()
        feishu["multirag_base_url"] = url

        with pytest.raises(ValidationError, match="requires https for non-loopback hosts"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    @pytest.mark.parametrize(
        "url,reason",
        [
            ("https://user:password@multirag.example.com", "must not contain userinfo"),
            ("https://multirag.example.com?target=other", "must not contain a query or fragment"),
            ("https://multirag.example.com#other", "must not contain a query or fragment"),
            ("https://multirag.example.com/api", "must use the origin root path"),
            ("https://multirag.example.com:invalid", "must contain a valid port"),
        ],
    )
    def test_multirag_base_url_rejects_ambiguous_components(self, url: str, reason: str) -> None:
        feishu = _enabled_feishu_config()
        feishu["multirag_base_url"] = url

        with pytest.raises(ValidationError, match=reason):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    def test_leader_renewal_must_precede_expiry(self) -> None:
        feishu = _enabled_feishu_config()
        feishu["leader_ttl_seconds"] = 10
        feishu["leader_renew_seconds"] = 10

        with pytest.raises(ValidationError, match="leader_renew_seconds must be less"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})

    def test_numeric_limits_must_be_positive(self) -> None:
        feishu = _enabled_feishu_config()
        feishu["queue_size"] = 0

        with pytest.raises(ValidationError, match=r"channels\.feishu\.queue_size"):
            AppConfig.model_validate({"channels": {"feishu": feishu}})


class TestChannelControlConfig:
    def test_default_is_disabled_by_empty_typed_secrets(self) -> None:
        control = AppConfig().channels.control

        assert isinstance(control.secret_encryption_key, SecretStr)
        assert isinstance(control.internal_api_token, SecretStr)
        assert control.secret_encryption_key.get_secret_value() == ""
        assert control.internal_api_token.get_secret_value() == ""
        assert control.runtime_api_base_url == ""
        assert control.session_ttl_seconds == 86_400
        assert control.dedupe_ttl_seconds == 86_400

    def test_accepts_urlsafe_aes256_key_and_long_internal_token(self) -> None:
        encoded_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        config = AppConfig.model_validate(
            {
                "channels": {
                    "control": {
                        "secret_encryption_key": encoded_key,
                        "internal_api_token": "i" * 32,
                    }
                }
            }
        )

        assert config.channels.control.secret_encryption_key.get_secret_value() == encoded_key
        assert config.channels.control.internal_api_token.get_secret_value() == "i" * 32
        assert encoded_key not in repr(config)

    @pytest.mark.parametrize(
        "encoded_key",
        ["not-base64!", base64.urlsafe_b64encode(b"too-short").decode("ascii")],
    )
    def test_rejects_invalid_encryption_key(self, encoded_key: str) -> None:
        with pytest.raises(ValidationError, match=r"channels\.control\.secret_encryption_key"):
            AppConfig.model_validate({"channels": {"control": {"secret_encryption_key": encoded_key}}})

    def test_rejects_short_internal_api_token(self) -> None:
        with pytest.raises(ValidationError, match=r"channels\.control\.internal_api_token"):
            AppConfig.model_validate({"channels": {"control": {"internal_api_token": "short"}}})

    @pytest.mark.parametrize(
        "url",
        ["http://127.0.0.1:8123", "http://localhost:8123", "https://multirag.example.com"],
    )
    def test_runtime_api_origin_accepts_local_http_or_remote_https(self, url: str) -> None:
        config = AppConfig.model_validate({"channels": {"control": {"runtime_api_base_url": url}}})

        assert config.channels.control.runtime_api_base_url == url

    @pytest.mark.parametrize(
        "url",
        ["http://10.0.0.8:8123", "https://user:pass@example.com", "https://example.com/api"],
    )
    def test_runtime_api_origin_rejects_unsafe_or_ambiguous_urls(self, url: str) -> None:
        with pytest.raises(ValidationError, match=r"channels\.control\.runtime_api_base_url"):
            AppConfig.model_validate({"channels": {"control": {"runtime_api_base_url": url}}})

    def test_control_environment_overlay(self, conf_dir: Callable[[str, str], None], monkeypatch: pytest.MonkeyPatch) -> None:
        encoded_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        conf_dir(SERVICE_CONF, "{}\n")
        monkeypatch.setenv("MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY", encoded_key)
        monkeypatch.setenv("MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN", "t" * 32)

        control = load_app_config().channels.control

        assert control.secret_encryption_key.get_secret_value() == encoded_key
        assert control.internal_api_token.get_secret_value() == "t" * 32


class TestFeishuEnvironmentOverlay:
    def test_nested_environment_values_build_typed_config(self, conf_dir: Callable[[str, str], None], monkeypatch: pytest.MonkeyPatch) -> None:
        conf_dir(SERVICE_CONF, "{}\n")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__ENABLED", "true")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__APP_ID", "cli_env")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__APP_SECRET", "env-app-secret")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__DOMAIN", "lark")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL", "http://127.0.0.1:9380")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__AGENT_ID", "agent_env")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN", "env-agent-token")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER", "release-env")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS", '["ou_env"]')
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__WORKER_CONCURRENCY", "3")

        feishu = load_app_config().channels.feishu

        assert feishu.enabled is True
        assert feishu.domain == "lark"
        assert feishu.allowed_open_ids == ["ou_env"]
        assert feishu.worker_concurrency == 3
        assert feishu.app_secret.get_secret_value() == "env-app-secret"
        assert feishu.agent_api_token.get_secret_value() == "env-agent-token"

    def test_enabled_env_configuration_fails_fast_with_path(self, conf_dir: Callable[[str, str], None], monkeypatch: pytest.MonkeyPatch) -> None:
        conf_dir(SERVICE_CONF, "{}\n")
        monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__ENABLED", "true")

        with pytest.raises(AppConfigError, match=r"channels\.feishu"):
            load_app_config()


class TestChannelConfigMasking:
    def test_channel_secrets_are_masked_without_mutating_source(self) -> None:
        source = {
            "channels": {
                "feishu": {
                    "app_secret": "app-secret-plaintext",
                    "api_token": "api-token-plaintext",
                    "agent_api_token": "agent-token-plaintext",
                    "app_id": "cli_demo",
                },
                "control": {
                    "internal_api_token": "internal-token-plaintext",
                    "secret_encryption_key": "encryption-key-plaintext",
                },
            }
        }

        masked = _mask_sensitive_fields(source)
        serialized = json.dumps(masked, ensure_ascii=False)

        assert masked["channels"]["feishu"]["app_secret"] == "********"
        assert masked["channels"]["feishu"]["api_token"] == "********"
        assert masked["channels"]["feishu"]["agent_api_token"] == "********"
        assert masked["channels"]["feishu"]["app_id"] == "cli_demo"
        assert masked["channels"]["control"]["internal_api_token"] == "********"
        assert masked["channels"]["control"]["secret_encryption_key"] == "********"
        assert "app-secret-plaintext" not in serialized
        assert "api-token-plaintext" not in serialized
        assert "agent-token-plaintext" not in serialized
        assert "internal-token-plaintext" not in serialized
        assert "encryption-key-plaintext" not in serialized
        assert source["channels"]["feishu"]["app_secret"] == "app-secret-plaintext"
        assert source["channels"]["feishu"]["api_token"] == "api-token-plaintext"
        assert source["channels"]["feishu"]["agent_api_token"] == "agent-token-plaintext"


def test_yaml_helpers_round_trip_utf8(tmp_path: Path) -> None:
    conf_path = tmp_path / "channel.yaml"
    source = {"channels": {"feishu": {"release_marker": "领导演示"}}}

    config_utils.rewrite_yaml_conf(str(conf_path), source)

    assert config_utils.load_yaml_conf(str(conf_path)) == source
