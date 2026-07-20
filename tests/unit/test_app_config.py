"""common/app_config.py 单元测试（配置重构 Phase 1）。

覆盖：来源优先级矩阵（base yaml < local 整体替换 < env）、嵌套 env 覆盖与类型
解析、校验 fail-fast、未建模 section 保留、default_models 解析与旧实现等价、
单例缓存语义。
"""

import textwrap

import pytest

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
