import copy
import importlib
import logging
import os
import stat
from pathlib import Path
from typing import Any

from filelock import FileLock
from ruamel.yaml import YAML

from common.constants import SERVICE_CONF
from common.file_utils import get_project_base_directory

EXTERNAL_CONFIG_OVERLAY_ENV = "MULTIRAG_CONFIG_OVERLAY_FILE"
_MAX_EXTERNAL_CONFIG_BYTES = 1_048_576


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise ValueError("external config overlay ownership cannot be verified")
    return int(getter())


def _load_external_config_overlay() -> dict[str, Any]:
    """Load one deployment-owned service config overlay without following links.

    The overlay may contain credentials, so POSIX deployments must give it to
    the current process owner only (0400/0600).  Reading through an already
    validated descriptor avoids a path replacement between validation and
    parsing.  Error messages deliberately omit both the path and file content.
    """

    configured_path = os.environ.get(EXTERNAL_CONFIG_OVERLAY_ENV)
    if configured_path is None:
        return {}
    if not configured_path or configured_path != configured_path.strip():
        raise ValueError("external config overlay path must be a canonical absolute path")

    path = Path(configured_path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("external config overlay path must be an absolute regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("external config overlay is unavailable") from None

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_EXTERNAL_CONFIG_BYTES:
            raise ValueError("external config overlay must be a non-empty regular file no larger than 1 MiB")
        if os.name != "nt":
            mode = stat.S_IMODE(metadata.st_mode)
            if mode not in {0o400, 0o600} or metadata.st_uid != _effective_user_id():
                raise ValueError("external config overlay must be owned by the process user with mode 0400 or 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_EXTERNAL_CONFIG_BYTES + 1)
        if len(raw) > _MAX_EXTERNAL_CONFIG_BYTES:
            raise ValueError("external config overlay must be a non-empty regular file no larger than 1 MiB")
    except ValueError:
        raise
    except OSError:
        raise ValueError("external config overlay is unavailable") from None
    finally:
        os.close(descriptor)

    try:
        yaml = YAML(typ="safe", pure=True)
        overlay = yaml.load(raw.decode("utf-8"))
    except Exception:
        raise ValueError("external config overlay is invalid YAML") from None

    if not isinstance(overlay, dict):
        raise ValueError("external config overlay must contain a mapping")
    return overlay


def load_yaml_conf(conf_path):
    if not os.path.isabs(conf_path):
        conf_path = os.path.join(get_project_base_directory(), conf_path)
    try:
        with open(conf_path, encoding="utf-8") as f:
            yaml = YAML(typ="safe", pure=True)
            return yaml.load(f)
    except Exception as e:
        raise OSError(f"loading yaml file config from {conf_path} failed:", e)


def rewrite_yaml_conf(conf_path, config):
    if not os.path.isabs(conf_path):
        conf_path = os.path.join(get_project_base_directory(), conf_path)
    try:
        with open(conf_path, "w", encoding="utf-8") as f:
            yaml = YAML(typ="safe")
            yaml.dump(config, f)
    except Exception as e:
        raise OSError(f"rewrite yaml file config {conf_path} failed:", e)


def conf_realpath(conf_name):
    conf_path = f"configs/{conf_name}"
    return os.path.join(get_project_base_directory(), conf_path)


def read_config(conf_name=SERVICE_CONF):
    local_config = {}
    local_path = conf_realpath(f"local.{conf_name}")

    # load local config file
    if os.path.exists(local_path):
        local_config = load_yaml_conf(local_path)
        if not isinstance(local_config, dict):
            raise ValueError(f'Invalid config file: "{local_path}".')

    global_config_path = conf_realpath(conf_name)
    global_config = load_yaml_conf(global_config_path)

    if not isinstance(global_config, dict):
        raise ValueError(f'Invalid config file: "{global_config_path}".')

    global_config.update(local_config)
    if conf_name == SERVICE_CONF:
        global_config.update(_load_external_config_overlay())
    return global_config


CONFIGS = read_config()


def _mask_sensitive_fields(config: Any, _already_copied: bool = False) -> Any:
    """递归处理配置字典，将敏感字段替换为 *

    Args:
        config: 配置字典
        _already_copied: 内部参数，标识是否已经深拷贝过
    """
    if not isinstance(config, dict):
        return config

    # 只在最外层执行一次深拷贝，避免重复拷贝
    if not _already_copied:
        config = copy.deepcopy(config)

    # 定义需要脱敏的字段列表
    sensitive_fields = {
        "password",
        "api_key",
        "api_token",
        "access_key",
        "secret_key",
        "secret",
        "app_secret",
        "agent_api_token",
        "internal_api_token",
        "secret_encryption_key",
        "sas_token",
        "client_secret",
        "http_secret_key",
        "private_key_file",
        "hmac_keyring",
        "payload_encryption_keys",
    }

    for key, value in config.items():
        # 敏感容器必须先按父键整体替换；否则 keyring 中任意 key id
        # 都会绕过逐字段名称匹配。
        if str(key).lower() in sensitive_fields:
            config[key] = "*" * 8
        # 如果值是字典，递归处理（传入 True 表示已经拷贝过）
        elif isinstance(value, dict):
            config[key] = _mask_sensitive_fields(value, _already_copied=True)

    return config


def show_configs():
    msg = f"Current configs, from {conf_realpath(SERVICE_CONF)}:"
    for k, v in CONFIGS.items():
        # 使用递归函数处理所有配置项
        masked_v = _mask_sensitive_fields(v) if isinstance(v, dict) else v
        msg += f"\n\t{k}: {masked_v}"
    # logging.info("默认不展示service_conf.yaml,如有需要,请至 api/utils/__init__.py 解开注释")
    logging.info(msg)


# def show_configs():
#     msg = f"Current configs, from {conf_realpath(SERVICE_CONF)}:"
#     for k, v in CONFIGS.items():
#         if isinstance(v, dict):
#             if "password" in v:
#                 v = copy.deepcopy(v)
#                 v["password"] = "*" * 8
#             if "access_key" in v:
#                 v = copy.deepcopy(v)
#                 v["access_key"] = "*" * 8
#             if "secret_key" in v:
#                 v = copy.deepcopy(v)
#                 v["secret_key"] = "*" * 8
#             if "secret" in v:
#                 v = copy.deepcopy(v)
#                 v["secret"] = "*" * 8
#             if "sas_token" in v:
#                 v = copy.deepcopy(v)
#                 v["sas_token"] = "*" * 8
#             if "oauth" in k:
#                 v = copy.deepcopy(v)
#                 for key, val in v.items():
#                     if "client_secret" in val:
#                         val["client_secret"] = "*" * 8
#             if "authentication" in k:
#                 v = copy.deepcopy(v)
#                 for key, val in v.items():
#                     if "http_secret_key" in val:
#                         val["http_secret_key"] = "*" * 8
#         msg += f"\n\t{k}: {v}"
#     logging.info(msg)


def get_base_config(key, default=None):
    if key is None:
        return None
    if default is None:
        default = os.environ.get(key.upper())
    return CONFIGS.get(key, default)


def decrypt_database_password(password):
    encrypt_password = get_base_config("encrypt_password", False)
    encrypt_module = get_base_config("encrypt_module", False)
    private_key = get_base_config("private_key", None)

    if not password or not encrypt_password:
        return password

    if not private_key:
        raise ValueError("No private key")

    module_fun = encrypt_module.split("#")
    pwdecrypt_fun = getattr(importlib.import_module(module_fun[0]), module_fun[1])

    return pwdecrypt_fun(private_key, password)


def decrypt_database_config(database=None, passwd_key="password", name="database"):
    if not database:
        database = get_base_config(name, {})

    database[passwd_key] = decrypt_database_password(database[passwd_key])
    return database


def update_config(key, value, conf_name=SERVICE_CONF):
    conf_path = conf_realpath(conf_name=conf_name)
    if not os.path.isabs(conf_path):
        conf_path = os.path.join(get_project_base_directory(), conf_path)

    with FileLock(os.path.join(os.path.dirname(conf_path), ".lock")):
        config = load_yaml_conf(conf_path=conf_path) or {}
        config[key] = value
        rewrite_yaml_conf(conf_path=conf_path, config=config)
