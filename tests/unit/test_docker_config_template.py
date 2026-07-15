import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from common.app_config import AppConfig

ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT_DIR / "docker" / "service_conf.yaml.template"
ENTRYPOINT_PATH = ROOT_DIR / "docker" / "entrypoint.sh"
TEMPLATE_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-")


def _render_config(tmp_path: Path, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    environment = os.environ.copy()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for variable_name in TEMPLATE_ENV_PATTERN.findall(template):
        environment.pop(variable_name, None)

    config_dir = tmp_path / "configs"
    environment.update(
        MULTIRAG_CONF_DIR=str(config_dir),
        MULTIRAG_TEMPLATE_DIR=str(TEMPLATE_PATH.parent),
        MULTIRAG_RENDER_CONFIG_ONLY="1",
        PYTHON_BIN=sys.executable,
        SKIP_CONFIG_GENERATE="0",
    )
    environment.update(overrides or {})

    subprocess.run(
        ["bash", str(ENTRYPOINT_PATH)],
        cwd=ROOT_DIR,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    rendered = yaml.safe_load((config_dir / "service_conf.yaml").read_text(encoding="utf-8"))
    assert isinstance(rendered, dict)
    return rendered


def test_default_docker_template_renders_valid_app_config(tmp_path: Path) -> None:
    rendered = _render_config(tmp_path)
    config = AppConfig.model_validate(rendered)

    assert config.gcs.bucket == "bridgtl-edm-d-bucket-multirag"
    assert config.smtp.mail_server == ""
    assert config.smtp.mail_username == ""
    assert config.smtp.mail_password == ""
    assert config.smtp.mail_default_sender == ["MultiRAG", ""]


def test_docker_template_yaml_escapes_environment_values(tmp_path: Path) -> None:
    postgres_password = "pa'ss\\word $HOME $(printf injected)"
    smtp_password = 'mail"\\secret: #literal $(printf injected)'
    dcs_access_key = "key: #literal"
    rendered = _render_config(
        tmp_path,
        {
            "DCS_ACCESS_KEY": dcs_access_key,
            "POSTGRES_PASSWORD": postgres_password,
            "POSTGRES_PORT": "6543",
            "SMTP_PASSWORD": smtp_password,
        },
    )
    config = AppConfig.model_validate(rendered)

    assert config.postgresql.password == postgres_password
    assert config.postgresql.port == 6543
    assert config.smtp.mail_password == smtp_password
    assert rendered["dcs_server"]["semantic_server"]["access_key"] == dcs_access_key
