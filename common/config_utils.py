import copy
import importlib
import logging
import os
from typing import Any

from filelock import FileLock
from ruamel.yaml import YAML

from common.constants import SERVICE_CONF
from common.file_utils import get_project_base_directory


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
    }

    for key, value in config.items():
        # 如果值是字典，递归处理（传入 True 表示已经拷贝过）
        if isinstance(value, dict):
            config[key] = _mask_sensitive_fields(value, _already_copied=True)
        # 如果键是敏感字段，替换为 *
        elif str(key).lower() in sensitive_fields:
            config[key] = "*" * 8

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
