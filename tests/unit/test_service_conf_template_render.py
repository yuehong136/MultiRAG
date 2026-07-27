"""docker/entrypoint.sh 渲染出来的 service_conf.yaml 必须能通过强类型配置校验。

`generate_config` 从 docker/service_conf.yaml.template 生成运行时配置，是容器部署里
唯一的配置来源（本地直跑不经过它，所以这条路很难被日常开发覆盖到）。而 AppConfig 是
强类型的：任何渲染成 YAML null 的字段都会让 pydantic 在 bootstrap 阶段 fail-fast，
api server / task executor / admin server 一起起不来。

真实事故：某部署上运行的旧版 entrypoint 用 `eval "echo \\"$line\\""` 渲染，行内双引号
被 shell 吃掉，`mail_server: "${SMTP_SERVER:-}"` 渲染成裸的 `mail_server:`（null），
容器起不来。当前版本改用受控的 Python 替换（支持空默认值、保留 YAML 引号、不用 eval），
这里通过 `MULTIRAG_RENDER_CONFIG_ONLY=1` 直接跑真实渲染路径把结果钉住。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from common.app_config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
TEMPLATE = REPO_ROOT / "docker" / "service_conf.yaml.template"

pytestmark = pytest.mark.skipif(
    not (ENTRYPOINT.exists() and TEMPLATE.exists() and shutil.which("bash")),
    reason="需要 docker/entrypoint.sh、模板与 bash",
)


def _render(tmp_path: Path, env: dict[str, str] | None = None) -> dict:
    """跑真实的 generate_config（MULTIRAG_RENDER_CONFIG_ONLY=1 渲染完即退出）。"""
    conf_dir = tmp_path / "configs"
    conf_dir.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MULTIRAG_RENDER_CONFIG_ONLY": "1",
            "MULTIRAG_CONF_DIR": str(conf_dir),
            "MULTIRAG_TEMPLATE_DIR": str(TEMPLATE.parent),
            "PYTHON_BIN": os.environ.get("PYTHON_BIN", "python3"),
            **(env or {}),
        },
    )
    rendered = conf_dir / "service_conf.yaml"
    assert proc.returncode == 0 and rendered.exists(), f"渲染失败 rc={proc.returncode}\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    return yaml.safe_load(rendered.read_text(encoding="utf-8"))


def test_rendered_config_passes_app_config_validation(tmp_path: Path) -> None:
    """最坏情况（一个环境变量都没设）下渲染的配置必须能通过 AppConfig 校验。"""
    AppConfig.model_validate(_render(tmp_path))  # 抛 ValidationError 即失败


def test_empty_defaults_render_as_strings_not_null(tmp_path: Path) -> None:
    """空默认值必须渲染成空字符串——裸空值是 YAML null，强类型配置会拒绝。"""
    data = _render(tmp_path)

    smtp = data["smtp"]
    for field in ("mail_server", "mail_username", "mail_password"):
        assert isinstance(smtp[field], str), f"smtp.{field} 渲染成了 {smtp[field]!r}（应为字符串）"
    assert all(isinstance(x, str) for x in smtp["mail_default_sender"]), smtp["mail_default_sender"]
    assert isinstance(data["gcs"], dict), f"gcs 渲染成了 {data['gcs']!r}"


def test_env_values_reach_the_rendered_config(tmp_path: Path) -> None:
    """设了环境变量要真的透传，别为了消灭 null 把占位符写死成默认值。"""
    data = _render(tmp_path, {"SMTP_SERVER": "smtp.example.org", "MINIO_BUCKET": "my-bucket"})

    assert data["smtp"]["mail_server"] == "smtp.example.org"
    assert data["minio"]["bucket"] == "my-bucket"


def test_values_are_not_re_interpreted(tmp_path: Path) -> None:
    """渲染不能二次解释取值——旧的 eval 实现会把 $ 与引号当代码执行。"""
    data = _render(tmp_path, {"SMTP_USERNAME": "a$(whoami)b", "MINIO_PREFIX_PATH": "p`id`q"})

    assert data["smtp"]["mail_username"] == "a$(whoami)b"
    assert data["minio"]["prefix_path"] == "p`id`q"
