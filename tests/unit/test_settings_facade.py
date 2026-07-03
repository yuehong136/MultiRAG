"""settings PEP 562 facade 行为测试（配置重构 Phase 2）。

验证：惰性配置项无需 init_settings 即可用、值与 app_config 一致、
引擎/存储门控语义、monkeypatch 兼容性、未知属性报错。
"""

import textwrap

import pytest

from common import config_utils, settings
from common.app_config import reset_app_config
from common.constants import SERVICE_CONF


@pytest.fixture
def conf_dir(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    monkeypatch.setattr(config_utils, "get_project_base_directory", lambda: str(tmp_path))
    reset_app_config()

    def write(name: str, content: str):
        (tmp_path / "configs" / name).write_text(textwrap.dedent(content), encoding="utf-8")

    yield write
    reset_app_config()


BASE_YAML = """
multirag:
  host: 10.1.2.3
  http_port: 7777
es:
  hosts: http://es:9200
  username: elastic
milvus:
  hosts: milvus:19530
minio:
  user: mi
  password: pw
smtp:
  mail_server: smtp.example.com
  mail_default_sender: [Sender, sender@example.com]
user_default_llm:
  factory: ZHIPU
  default_models:
    chat_model: glm-4
"""


class TestLazyConfigNoInitRequired:
    """核心目标：纯配置项在不调用 init_settings 的情况下即可正确取值。"""

    def test_host_and_port(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        assert settings.HOST_IP == "10.1.2.3"
        assert settings.HOST_PORT == 7777

    def test_smtp_values(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        assert settings.MAIL_SERVER == "smtp.example.com"
        assert settings.MAIL_DEFAULT_SENDER == ("Sender", "sender@example.com")
        assert settings.MAIL_PORT == 0  # 未配置回落模型默认

    def test_resolved_model_configs(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        assert settings.CHAT_MDL == "glm-4@ZHIPU"
        assert settings.CHAT_CFG["factory"] == "ZHIPU"
        assert settings.LLM_FACTORY == "ZHIPU"

    def test_factory_llm_infos_is_list_without_init(self, conf_dir):
        conf_dir(SERVICE_CONF, BASE_YAML)

        # 曾经的启动崩溃类别：未 init 时为 None。facade 下永远是 list。
        assert isinstance(settings.FACTORY_LLM_INFOS, list)


class TestEngineAndStorageGating:
    def test_selected_engine_section_visible_others_empty(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("DOC_ENGINE", "elasticsearch")

        assert settings.DOC_ENGINE == "elasticsearch"
        assert settings.ES["hosts"] == "http://es:9200"
        assert settings.MILVUS == {}

    def test_switching_engine_env_switches_sections(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("DOC_ENGINE", "milvus")

        assert settings.ES == {}
        assert settings.MILVUS["hosts"] == "milvus:19530"
        assert settings.DOC_ENGINE_INFINITY is False

    def test_infinity_default_when_selected_but_unconfigured(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("DOC_ENGINE", "infinity")

        assert settings.INFINITY["uri"] == "infinity:23817"
        assert settings.DOC_ENGINE_INFINITY is True

    def test_storage_gating_default_minio(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.delenv("STORAGE_IMPL", raising=False)

        assert settings.STORAGE_IMPL_TYPE == "MINIO"
        assert settings.MINIO["user"] == "mi"
        assert settings.S3 == {}
        assert settings.AZURE == {}


class TestEnvDynamicValues:
    def test_embedding_mdl_tei_profile_override(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("COMPOSE_PROFILES", "tei-cpu")
        monkeypatch.setenv("TEI_MODEL", "my/tei-model")

        assert settings.EMBEDDING_MDL == "my/tei-model"

    def test_register_enabled_env(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("REGISTER_ENABLED", "0")

        assert settings.REGISTER_ENABLED == 0

    def test_disable_password_login_env_wins(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("DISABLE_PASSWORD_LOGIN", "true")

        assert settings.DISABLE_PASSWORD_LOGIN is True

    def test_doc_maximum_size_env(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)
        monkeypatch.setenv("MAX_CONTENT_LENGTH", "1024")

        assert settings.DOC_MAXIMUM_SIZE == 1024


class TestMonkeypatchCompatibility:
    """存量测试大量 monkeypatch.setattr(settings, "X", ...)——语义必须保持。"""

    def test_setattr_shadows_lazy_and_survives_undo(self, conf_dir, monkeypatch):
        conf_dir(SERVICE_CONF, BASE_YAML)

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(settings, "HOST_IP", "patched-host")
                assert settings.HOST_IP == "patched-host"

            # undo 后仍可访问（monkeypatch 会把补丁时取到的惰性值写回模块 dict；
            # 生产配置不可变因此语义等价——但本测试用的是 tmp 配置，必须清掉
            # 写回的冻结值以免污染后续测试）
            assert settings.HOST_IP == "10.1.2.3"
        finally:
            if "HOST_IP" in vars(settings):
                delattr(settings, "HOST_IP")

    def test_uninitialized_core_resource_fails_fast(self):
        from common import resources

        resources.reset_resources()
        with pytest.raises(resources.ResourcesNotInitialized, match="ensure_initialized"):
            _ = settings.docStoreConn
        with pytest.raises(resources.ResourcesNotInitialized, match="ensure_initialized"):
            _ = settings.STORAGE_IMPL

    def test_uninitialized_secret_key_is_none_for_apps_guard(self):
        # api/apps 的模块级守卫依赖"未初始化时 SECRET_KEY 为 None"语义
        from common import resources

        resources.reset_resources()
        assert settings.SECRET_KEY is None
        assert settings.msgStoreConn is None


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError, match="NO_SUCH_SETTING"):
        _ = settings.NO_SUCH_SETTING


def test_dir_includes_lazy_names():
    listing = dir(settings)
    assert "HOST_IP" in listing
    assert "docStoreConn" in listing
