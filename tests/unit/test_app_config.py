"""common/app_config.py 单元测试（配置重构 Phase 1）。

覆盖：来源优先级矩阵（base yaml < local 整体替换 < env）、嵌套 env 覆盖与类型
解析、校验 fail-fast、未建模 section 保留、default_models 解析与旧实现等价、
单例缓存语义。
"""

import base64
import json
import textwrap
import traceback

import pytest
from pydantic import ValidationError

from common import app_config, config_utils
from common.app_config import AppConfigError, get_app_config, load_app_config, reset_app_config
from common.constants import SERVICE_CONF


@pytest.fixture
def conf_dir(tmp_path, monkeypatch):
    """配置根指向 tmp，并保证前后缓存干净。"""
    (tmp_path / "configs").mkdir()
    monkeypatch.setattr(config_utils, "get_project_base_directory", lambda: str(tmp_path))
    reset_app_config()

    def write(name: str, content: str):
        (tmp_path / "configs" / name).write_text(textwrap.dedent(content), encoding="utf-8")

    yield write
    reset_app_config()


BASE_YAML = """
multirag:
  host: 0.0.0.0
  http_port: 8123
postgresql:
  user: usr_ai
  password: base-pass
  host: db.internal
  port: 5432
tcadp_config:
  region: ap-shanghai
  secret_id: sid
"""


def _encoded_test_hmac_key(offset: int, *, length: int = 32, padded: bool = True) -> str:
    """Return deterministic non-production material for config parsing tests."""
    material = bytes((offset + index) % 256 for index in range(length))
    encoded = base64.urlsafe_b64encode(material).decode("ascii")
    return encoded if padded else encoded.rstrip("=")


class TestSourcePrecedence:
    def test_base_yaml_only(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        cfg = load_app_config()

        assert cfg.multirag.host == "0.0.0.0"
        assert cfg.multirag.http_port == 8123
        assert cfg.postgresql.user == "usr_ai"

    def test_local_replaces_whole_section(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)
        conf_dir(
            f"local.{SERVICE_CONF}",
            """
            multirag:
              host: 127.0.0.1
            """,
        )

        cfg = load_app_config()

        assert cfg.multirag.host == "127.0.0.1"
        # section 整体替换：local 未写的 http_port 回落到模型默认值，而非 base 的 8123
        assert cfg.multirag.http_port == ServerDefaults.HTTP_PORT
        # 未触碰的 section 不受影响
        assert cfg.postgresql.password == "base-pass"

    def test_env_beats_local_and_base(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        conf_dir(f"local.{SERVICE_CONF}", "multirag: {host: 127.0.0.1, http_port: 9000}\n")
        monkeypatch.setenv("MULTIRAG_MULTIRAG__HTTP_PORT", "9999")
        monkeypatch.setenv("MULTIRAG_POSTGRESQL__PASSWORD", "env-pass")

        cfg = load_app_config()

        assert cfg.multirag.http_port == 9999  # env 覆盖 local
        assert cfg.multirag.host == "127.0.0.1"  # env 未覆盖的字段保持 local
        assert cfg.postgresql.password == "env-pass"  # env 覆盖 base


class TestEnvOverlay:
    def test_nested_path_creates_dicts(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_AUTHENTICATION__CLIENT__SWITCH", "true")

        cfg = load_app_config()

        assert cfg.authentication.client == {"switch": True}

    def test_yaml_scalar_coercion(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_MULTIRAG__ADMIN_REQUIRE_SUPERUSER", "true")
        monkeypatch.setenv("MULTIRAG_REDIS__DB", "3")
        monkeypatch.setenv("MULTIRAG_REDIS__HOST", "10.0.0.1:6380")

        cfg = load_app_config()

        assert cfg.multirag.admin_require_superuser is True
        assert cfg.redis.db == 3
        assert cfg.redis.host == "10.0.0.1:6380"

    def test_bare_prefix_var_without_delimiter_is_ignored(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_DEBUGPY", "1")  # 无 __，不是配置覆盖

        cfg = load_app_config()

        assert cfg.get_section("debugpy") is None

    def test_blank_env_value_clears_a_field_instead_of_killing_the_load(self, conf_dir, monkeypatch):
        """CHN-O12：留空的 env 变量是「空值」，不是「非法值」。

        env 只能传字符串，所以「把这个字段设成空」只有留空一种写法，而
        `yaml.safe_load("")` 是 `None`——打在 `str` 字段上就是 ValidationError，
        整个进程起不来。`docker/docker-compose.yml` 正是用留空表示「不启用 channel」。
        """
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_POSTGRESQL__PASSWORD", "")

        cfg = load_app_config()

        assert cfg.postgresql.password == ""

    def test_blank_env_value_leaves_a_nullable_field_null(self, conf_dir, monkeypatch):
        """收敛只针对类型容不下 None 的字段。

        `secret_key: str | None` 的 `None` 是有意义的取值——「没有配」不等于
        「配成了空字符串」。把它一起收成 `""` 才是真的改坏语义。
        """
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_MULTIRAG__SECRET_KEY", "")

        cfg = load_app_config()

        assert cfg.multirag.secret_key is None

    def test_infinity_pool_size_supports_upstream_env_and_typed_override(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("INFINITY_POOL_MAX_SIZE", "12")

        cfg = load_app_config()

        assert cfg.infinity.pool_max_size == 12

        monkeypatch.setenv("MULTIRAG_INFINITY__POOL_MAX_SIZE", "20")
        cfg = load_app_config()

        assert cfg.infinity.pool_max_size == 20


class TestValidation:
    def test_type_error_fails_fast_with_field_path(self, conf_dir):
        conf_dir(SERVICE_CONF, "multirag: {http_port: not-a-port}\n")

        with pytest.raises(AppConfigError, match=r"multirag\.http_port"):
            load_app_config()

    def test_infinity_pool_size_must_be_positive(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("INFINITY_POOL_MAX_SIZE", "0")

        with pytest.raises(AppConfigError, match=r"infinity\.pool_max_size"):
            load_app_config()

    def test_unmodeled_section_preserved(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        cfg = load_app_config()

        assert cfg.get_section("tcadp_config") == {"region": "ap-shanghai", "secret_id": "sid"}
        assert cfg.get_section("no_such_section", {"d": 1}) == {"d": 1}

    def test_unmodeled_field_in_known_section_preserved(self, conf_dir):
        conf_dir(SERVICE_CONF, "multirag: {host: 0.0.0.0, future_upstream_key: 42}\n")

        cfg = load_app_config()

        # 上游新增的未建模字段：extra=allow 保留在模型上 + raw 可取
        assert cfg.multirag.future_upstream_key == 42
        assert cfg.raw["multirag"]["future_upstream_key"] == 42

    def test_vastbase_schema_key_maps_to_schema_(self, conf_dir):
        conf_dir(SERVICE_CONF, "vastbase: {schema: my_schema, user: u}\n")

        cfg = load_app_config()

        assert cfg.vastbase.schema_ == "my_schema"


class TestIdentityProvisioningConfig:
    def test_pydantic_validation_traceback_redacts_rejected_hmac_material(self):
        marker = "LEAK-MARKER-identity-HMAC"

        with pytest.raises(ValidationError) as raised:
            app_config.AppConfig.model_validate(
                {"identity": {"provisioning": {"hmac_keyring": {"active": marker}}}},
            )

        error = raised.value
        assert error.__cause__ is None
        assert error.__context__ is None
        assert marker not in str(error)
        assert marker not in repr(error)
        assert marker not in "".join(traceback.format_exception(error))

    def test_yaml_parses_active_and_retired_hmac_keys(self, conf_dir):
        active = _encoded_test_hmac_key(0)
        retired = _encoded_test_hmac_key(32, length=48, padded=False)
        conf_dir(
            SERVICE_CONF,
            f"""
            identity:
              provisioning:
                active_key_id: active_2026
                hmac_keyring:
                  active_2026: "{active}"
                  retired-2025: "{retired}"
            """,
        )

        cfg = load_app_config()

        assert cfg.identity.provisioning.active_key_id == "active_2026"
        assert cfg.identity.provisioning.require_hmac_keyring() == {
            "active_2026": bytes(range(32)),
            "retired-2025": bytes(range(32, 80)),
        }

    def test_env_parses_nested_identity_keyring(self, conf_dir, monkeypatch):
        active = _encoded_test_hmac_key(80)
        retired = _encoded_test_hmac_key(112)
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MULTIRAG_IDENTITY__PROVISIONING__ACTIVE_KEY_ID", "active-v2")
        monkeypatch.setenv(
            "MULTIRAG_IDENTITY__PROVISIONING__HMAC_KEYRING",
            json.dumps({"active-v2": active, "retired_v1": retired}),
        )

        provisioning = load_app_config().identity.provisioning

        assert provisioning.active_key_id == "active-v2"
        assert set(provisioning.require_hmac_keyring()) == {"active-v2", "retired_v1"}

    def test_empty_identity_config_preserves_startup_but_runtime_fails_fast(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        provisioning = load_app_config().identity.provisioning

        assert provisioning.active_key_id == ""
        assert provisioning.hmac_keyring == {}
        with pytest.raises(AppConfigError, match=r"identity\.provisioning\.active_key_id"):
            provisioning.require_hmac_keyring()

    def test_keyring_without_active_id_only_fails_when_identity_is_enabled(self, conf_dir):
        key = _encoded_test_hmac_key(144)
        conf_dir(
            SERVICE_CONF,
            f'identity: {{provisioning: {{hmac_keyring: {{retired: "{key}"}}}}}}\n',
        )

        provisioning = load_app_config().identity.provisioning

        with pytest.raises(AppConfigError, match=r"identity\.provisioning\.active_key_id"):
            provisioning.require_hmac_keyring()

    def test_active_id_must_resolve_in_keyring_when_identity_is_enabled(self, conf_dir):
        key = _encoded_test_hmac_key(176)
        conf_dir(
            SERVICE_CONF,
            f'identity: {{provisioning: {{active_key_id: active, hmac_keyring: {{retired: "{key}"}}}}}}\n',
        )

        provisioning = load_app_config().identity.provisioning

        with pytest.raises(AppConfigError, match="must name a key in hmac_keyring"):
            provisioning.require_hmac_keyring()

    @pytest.mark.parametrize("key_id", ["", "has space", "nonascii-é", "x" * 65, "dot.not.allowed"])
    def test_keyring_rejects_invalid_key_ids(self, conf_dir, key_id):
        key = _encoded_test_hmac_key(208)
        conf_dir(
            SERVICE_CONF,
            "identity:\n  provisioning:\n    hmac_keyring: " + json.dumps({key_id: key}) + "\n",
        )

        with pytest.raises(AppConfigError, match=r"identity\.provisioning\.hmac_keyring"):
            load_app_config()

    @pytest.mark.parametrize("key_id", ["has space", "nonascii-é", "x" * 65, "dot.not.allowed"])
    def test_active_key_id_rejects_invalid_values(self, conf_dir, key_id):
        conf_dir(
            SERVICE_CONF,
            "identity:\n  provisioning:\n    active_key_id: " + json.dumps(key_id) + "\n",
        )

        with pytest.raises(AppConfigError, match=r"identity\.provisioning\.active_key_id"):
            load_app_config()

    @pytest.mark.parametrize("invalid", [_encoded_test_hmac_key(0, length=31), "not+canonical/base64"])
    def test_keyring_rejects_weak_or_malformed_material_without_echoing_it(self, conf_dir, invalid):
        conf_dir(
            SERVICE_CONF,
            f'identity: {{provisioning: {{hmac_keyring: {{weak: "{invalid}"}}}}}}\n',
        )

        with pytest.raises(AppConfigError, match=r"identity\.provisioning\.hmac_keyring") as raised:
            load_app_config()

        error = raised.value
        assert error.__cause__ is None
        assert error.__context__ is None
        assert invalid not in str(error)
        assert invalid not in repr(error)
        assert invalid not in "".join(traceback.format_exception(error))

    def test_identity_secrets_are_redacted_from_repr_and_json_dump(self, conf_dir):
        secret = _encoded_test_hmac_key(16)
        conf_dir(
            SERVICE_CONF,
            f'identity: {{provisioning: {{active_key_id: active, hmac_keyring: {{active: "{secret}"}}}}}}\n',
        )

        cfg = load_app_config()
        rendered = repr(cfg.identity.provisioning)
        dumped = cfg.model_dump(mode="json")

        assert secret not in rendered
        assert secret not in json.dumps(dumped)
        assert dumped["identity"]["provisioning"]["hmac_keyring"]["active"] == "**********"


class TestMcpIssuerConfig:
    def test_empty_issuer_config_preserves_startup_but_runtime_fails_closed(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        issuer = load_app_config().identity.mcp_issuer

        assert issuer.enabled is False
        with pytest.raises(AppConfigError, match=r"identity\.mcp_issuer is disabled"):
            issuer.require_enabled()

    def test_enabled_file_issuer_parses_fixed_authority_and_redacts_private_path(self, conf_dir):
        private_path = "/run/secrets/eim-a2-private-marker.pem"
        conf_dir(
            SERVICE_CONF,
            f"""
            identity:
              mcp_issuer:
                enabled: true
                issuer: https://auth.multirag.example
                client_id: multirag-first-party
                ttl_seconds: 300
                jwks_cache_ttl_seconds: 300
                resources:
                  ofmcp_gateway:
                    audience: https://gateway.ofmcp.example/mcp
                    registered_scopes: [leave:read, leave:submit]
                    enterprise_subject:
                      subject_type: workcode
                      issuer: https://hr.example
                      issuer_tenant: issuer-tenant-a
                key_provider:
                  kind: file
                  active_key_id: current-2026
                  private_key_file: {private_path}
                  public_key_files:
                    current-2026: /etc/multirag/current.pub.pem
                    next-2026: /etc/multirag/next.pub.pem
            """,
        )

        cfg = load_app_config()
        issuer = cfg.identity.mcp_issuer.require_enabled()
        dumped = cfg.model_dump(mode="json")

        assert issuer.resources["ofmcp_gateway"].registered_scopes == ["leave:read", "leave:submit"]
        assert issuer.resources["ofmcp_gateway"].enterprise_subject.subject_type == "workcode"
        assert issuer.key_provider.active_key_id == "current-2026"
        assert issuer.minimum_retired_key_retention_seconds == 630
        assert private_path not in repr(issuer)
        assert private_path not in json.dumps(dumped)
        assert dumped["identity"]["mcp_issuer"]["key_provider"]["private_key_file"] == "**********"
        masked = config_utils._mask_sensitive_fields(
            {"identity": {"mcp_issuer": {"private_key_file": private_path}}},
        )
        assert private_path not in repr(masked)

    @pytest.mark.parametrize(
        "fragment",
        [
            "issuer: http://auth.local",
            "issuer: https://user@auth.example",
            "issuer: https://auth.example?tenant=a",
            "ttl_seconds: 301",
            "jwks_cache_ttl_seconds: 0",
        ],
    )
    def test_enabled_issuer_rejects_invalid_security_boundary(self, conf_dir, fragment):
        body = """
        identity:
          mcp_issuer:
            enabled: true
            issuer: https://auth.multirag.example
            client_id: multirag-first-party
            ttl_seconds: 300
            jwks_cache_ttl_seconds: 300
            resources:
              gateway:
                audience: https://gateway.ofmcp.example/mcp
                registered_scopes: [leave:read]
            key_provider:
              kind: file
              active_key_id: current
              private_key_file: /run/secrets/current.pem
              public_key_files: {current: /etc/current.pub.pem}
        """
        body = (
            body.replace("issuer: https://auth.multirag.example", fragment)
            if fragment.startswith("issuer:")
            else body.replace("ttl_seconds: 300", fragment, 1)
            if fragment.startswith("ttl")
            else body.replace("jwks_cache_ttl_seconds: 300", fragment)
        )
        conf_dir(SERVICE_CONF, body)

        with pytest.raises(AppConfigError, match=r"identity\.mcp_issuer"):
            load_app_config()

    def test_resource_registry_rejects_unknown_claim_and_duplicate_scope(self, conf_dir):
        conf_dir(
            SERVICE_CONF,
            """
            identity:
              mcp_issuer:
                enabled: true
                issuer: https://auth.multirag.example
                client_id: multirag-first-party
                resources:
                  gateway:
                    audience: https://gateway.ofmcp.example/mcp
                    registered_scopes: [leave:read, leave:read]
                    allowed_claims: [roles]
                key_provider:
                  active_key_id: current
                  private_key_file: /run/secrets/current.pem
                  public_key_files: {current: /etc/current.pub.pem}
            """,
        )

        with pytest.raises(AppConfigError, match=r"identity\.mcp_issuer"):
            load_app_config()


class TestDefaultModelsResolutionParity:
    """resolved_model 与旧 settings._resolve_per_model_config 逐条等价。"""

    CASES = [
        # (default_models entry, top-level factory/api_key/base_url, expected model)
        ("glm-4", ("ZHIPU", "k", "http://b"), "glm-4@ZHIPU"),
        ({"name": "glm-4", "factory": "MINE"}, ("BACKUP", None, None), "glm-4@MINE"),
        ({"name": "glm-4@X"}, ("BACKUP", None, None), "glm-4@X"),
        ({"model": "via-model-key"}, ("F", None, None), "via-model-key@F"),
        ("", ("F", None, None), ""),
        (42, ("F", None, None), ""),
    ]

    @pytest.mark.parametrize("entry,tops,expected_model", CASES)
    def test_parity_with_legacy_resolver(self, entry, tops, expected_model):
        from common.settings import _parse_model_entry as legacy_parse
        from common.settings import _resolve_per_model_config as legacy_resolve

        factory, api_key, base_url = tops
        llm = app_config.UserDefaultLLMConfig(
            factory=factory or "",
            api_key=api_key,
            base_url=base_url or "",
            default_models={"chat_model": entry},
        )

        resolved = llm.resolved_model("chat_model")
        legacy = legacy_resolve(legacy_parse(entry), factory, api_key, base_url)

        assert resolved.model == expected_model
        assert resolved.as_dict() == legacy

    def test_missing_kind_resolves_to_empty(self):
        llm = app_config.UserDefaultLLMConfig(factory="F")
        assert llm.resolved_model("no_such_kind").model == ""


class TestSingleton:
    def test_get_app_config_caches(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        assert get_app_config() is get_app_config()

    def test_reset_clears_cache(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)
        first = get_app_config()
        reset_app_config()

        assert get_app_config() is not first


class ServerDefaults:
    HTTP_PORT = app_config.ServerConfig().http_port
