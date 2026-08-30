"""行为钉板：configs/service_conf.yaml 的读取与覆盖语义。

配置重构（internal/config_bootstrap_refactor_plan.md）期间，以下现状行为
不允许改变：
1. configs/local.service_conf.yaml 按**顶层 section 整体替换** base 配置
   （dict.update 语义，非字段级深合并）——CI 与本地覆盖文件依赖此语义；
2. get_base_config 仅在 key 完全不存在于 CONFIGS 时才回退到同名大写环境变量。
"""

import textwrap
from pathlib import Path

import pytest

from common import config_utils
from common.constants import SERVICE_CONF


@pytest.fixture
def conf_dir(tmp_path, monkeypatch):
    """把配置根目录指到 tmp，写入受控的 base/local 双文件。"""
    (tmp_path / "configs").mkdir()
    monkeypatch.setattr(config_utils, "get_project_base_directory", lambda: str(tmp_path))

    def write(name: str, content: str):
        (tmp_path / "configs" / name).write_text(textwrap.dedent(content), encoding="utf-8")

    return write


def test_local_override_replaces_whole_section(conf_dir):
    conf_dir(
        SERVICE_CONF,
        """
        multirag:
          host: 0.0.0.0
          http_port: 8123
          secret_key: base-secret
        redis:
          host: 127.0.0.1:6379
          db: 1
        """,
    )
    conf_dir(
        f"local.{SERVICE_CONF}",
        """
        multirag:
          host: 127.0.0.1
        """,
    )

    merged = config_utils.read_config(SERVICE_CONF)

    # 被覆盖的 section 整体替换：local 未写的字段（http_port/secret_key）随之消失
    assert merged["multirag"] == {"host": "127.0.0.1"}
    # 未触碰的 section 原样保留
    assert merged["redis"] == {"host": "127.0.0.1:6379", "db": 1}


def test_no_local_file_uses_base_only(conf_dir):
    conf_dir(
        SERVICE_CONF,
        """
        multirag:
          host: 0.0.0.0
        """,
    )

    merged = config_utils.read_config(SERVICE_CONF)

    assert merged == {"multirag": {"host": "0.0.0.0"}}


def test_non_dict_local_config_raises(conf_dir):
    conf_dir(SERVICE_CONF, "multirag: {host: 0.0.0.0}\n")
    conf_dir(f"local.{SERVICE_CONF}", "- just\n- a\n- list\n")

    with pytest.raises(ValueError, match="Invalid config file"):
        config_utils.read_config(SERVICE_CONF)


def test_external_overlay_replaces_local_whole_sections(conf_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf_dir(SERVICE_CONF, "multirag: {host: base, http_port: 8123}\nredis: {db: 1}\n")
    conf_dir(f"local.{SERVICE_CONF}", "multirag: {host: local, http_port: 9000}\n")
    overlay = tmp_path / "deployment.yaml"
    overlay.write_text("multirag: {host: external}\n", encoding="utf-8")
    overlay.chmod(0o600)
    monkeypatch.setenv(config_utils.EXTERNAL_CONFIG_OVERLAY_ENV, str(overlay))

    merged = config_utils.read_config(SERVICE_CONF)

    assert merged == {"multirag": {"host": "external"}, "redis": {"db": 1}}


def test_external_overlay_does_not_apply_to_arbitrary_config_files(conf_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf_dir("admin.yaml", "admin: {host: original}\n")
    overlay = tmp_path / "deployment.yaml"
    overlay.write_text("admin: {host: injected}\n", encoding="utf-8")
    overlay.chmod(0o600)
    monkeypatch.setenv(config_utils.EXTERNAL_CONFIG_OVERLAY_ENV, str(overlay))

    assert config_utils.read_config("admin.yaml") == {"admin": {"host": "original"}}


def test_external_overlay_rejects_relative_symlink_and_unsafe_mode(conf_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf_dir(SERVICE_CONF, "multirag: {host: base}\n")
    target = tmp_path / "target.yaml"
    target.write_text("multirag: {host: external}\n", encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / "overlay.yaml"
    link.symlink_to(target)

    monkeypatch.setenv(config_utils.EXTERNAL_CONFIG_OVERLAY_ENV, "relative.yaml")
    with pytest.raises(ValueError, match="absolute regular"):
        config_utils.read_config(SERVICE_CONF)

    monkeypatch.setenv(config_utils.EXTERNAL_CONFIG_OVERLAY_ENV, str(link))
    with pytest.raises(ValueError, match="non-symlink"):
        config_utils.read_config(SERVICE_CONF)

    monkeypatch.setenv(config_utils.EXTERNAL_CONFIG_OVERLAY_ENV, str(target))
    with pytest.raises(ValueError, match="0400 or 0600"):
        config_utils.read_config(SERVICE_CONF)


def test_get_base_config_prefers_configs_over_env(monkeypatch):
    monkeypatch.setitem(config_utils.CONFIGS, "characterize_key", {"value": 1})
    monkeypatch.setenv("CHARACTERIZE_KEY", "from-env")

    assert config_utils.get_base_config("characterize_key") == {"value": 1}


def test_get_base_config_falls_back_to_env_only_when_key_absent(monkeypatch):
    monkeypatch.delitem(config_utils.CONFIGS, "characterize_key", raising=False)
    monkeypatch.setenv("CHARACTERIZE_KEY", "from-env")

    assert config_utils.get_base_config("characterize_key") == "from-env"


def test_get_base_config_explicit_default_beats_env(monkeypatch):
    monkeypatch.delitem(config_utils.CONFIGS, "characterize_key", raising=False)
    monkeypatch.setenv("CHARACTERIZE_KEY", "from-env")

    assert config_utils.get_base_config("characterize_key", {"d": 1}) == {"d": 1}
