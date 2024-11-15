# coding=utf-8
"""
@project: multirag
@Author：龙
@file： runtime_config.py
@date：2024/7/9 9:00
@desc:
"""
from api.versions import get_multirag_version
from .reload_config_base import ReloadConfigBase


class RuntimeConfig(ReloadConfigBase):
    DEBUG = None
    WORK_MODE = None
    HTTP_PORT = None
    JOB_SERVER_HOST = None
    JOB_SERVER_VIP = None
    ENV = dict()
    SERVICE_DB = None
    LOAD_CONFIG_MANAGER = False

    @classmethod
    def init_config(cls, **kwargs):
        for k, v in kwargs.items():
            if hasattr(cls, k):
                setattr(cls, k, v)

    @classmethod
    def init_env(cls):
        cls.ENV.update({"version": get_multirag_version()})

    @classmethod
    def load_config_manager(cls):
        cls.LOAD_CONFIG_MANAGER = True

    @classmethod
    def get_env(cls, key):
        return cls.ENV.get(key, None)

    @classmethod
    def get_all_env(cls):
        return cls.ENV

    @classmethod
    def set_service_db(cls, service_db):
        cls.SERVICE_DB = service_db
