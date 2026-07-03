"""全局配置/资源兼容 facade（配置重构 Phase 2+3，方案见 internal/config_bootstrap_refactor_plan.md）。

本模块是 **上游兼容面**：610+ 存量调用点与 ragflow 上游移植 diff 继续使用
``settings.X`` 访问方式，值由 PEP 562 模块 ``__getattr__`` 惰性委托：

- 纯配置项（HOST_IP、MAIL_*、CHAT_CFG…）→ :mod:`common.app_config`
  （类型化、不可变、**无需初始化**随取随算）；
- 资源句柄（docStoreConn、STORAGE_IMPL、retriever、SECRET_KEY…）→
  :mod:`common.resources`（入口点先调 ``common.bootstrap.ensure_initialized()``；
  核心资源未初始化即访问会 fail-fast 抛错，而不是默默给 None）。

本模块**不再包含任何重型 import**（向量库/存储后端按选中项在 resources 中
懒加载）——重构前 ``import common.settings`` 约 8 秒，现为毫秒级。

公开名字清单受 tests/unit/test_settings_facade_inventory.py 契约保护，
移植映射见 internal/ragflow_settings_porting_map.md。

注意：对本模块 ``settings.X = value`` 的写入（含 monkeypatch）会在模块 dict
写入真实属性、遮蔽同名惰性值；tests/unit/conftest.py 的 autouse fixture
会在每个测试后清扫遮蔽。生产代码禁止写入。
"""

import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

from common import bootstrap, resources
from common.app_config import get_app_config, get_factory_llm_infos

# 纯 re-export（facade 公开契约的一部分，受 inventory 测试保护）：
# 冗余别名写法向 ruff 声明这是有意导出，勿清理
from common.config_utils import decrypt_database_config as decrypt_database_config
from common.config_utils import get_base_config as get_base_config
from common.constants import MULTI_RAG_SERVICE_NAME as MULTI_RAG_SERVICE_NAME
from common.constants import SVR_QUEUE_NAME
from common.constants import Storage as Storage
from common.file_utils import get_project_base_directory
from common.misc_utils import pip_install_torch
from common.resources import StorageFactory as StorageFactory

# ---------------------------------------------------------------------------
# import 期冻结的环境常量（与历史行为一致）
# ---------------------------------------------------------------------------

LIGHTEN = int(os.environ.get("LIGHTEN", "0"))
STRONG_TEST_COUNT = int(os.environ.get("STRONG_TEST_COUNT", "8"))
BUILTIN_EMBEDDING_MODELS = ["BAAI/bge-large-zh-v1.5@BAAI", "maidalun1020/bce-embedding-base_v1@Youdao"]
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")

# ---------------------------------------------------------------------------
# 历史遗留的普通全局（钉板保持）
# ---------------------------------------------------------------------------

LLM = None  # 历史遗留，无消费者
AUTHENTICATION_CONF = None  # 历史遗留：旧 init_settings 从未赋值，保持 None
PARALLEL_DEVICES: int = 0  # 由 check_and_install_torch 填充


def get_svr_queue_name(priority: int) -> str:
    if priority == 0:
        return SVR_QUEUE_NAME
    return f"{SVR_QUEUE_NAME}_{priority}"


def get_svr_queue_names():
    return [get_svr_queue_name(priority) for priority in [1, 0]]


# ---------------------------------------------------------------------------
# 旧模型条目解析器（逻辑已收编 app_config.UserDefaultLLMConfig，此处保留供
# 存量导入与 characterization 测试钉板使用）
# ---------------------------------------------------------------------------


def _parse_model_entry(entry):
    if isinstance(entry, str):
        return {"name": entry, "factory": None, "api_key": None, "base_url": None}
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("model") or ""
        return {
            "name": name,
            "factory": entry.get("factory"),
            "api_key": entry.get("api_key"),
            "base_url": entry.get("base_url"),
        }
    return {"name": "", "factory": None, "api_key": None, "base_url": None}


def _resolve_per_model_config(entry_dict, backup_factory, backup_api_key, backup_base_url):
    name = (entry_dict.get("name") or "").strip()
    m_factory = entry_dict.get("factory") or backup_factory or ""
    m_api_key = entry_dict.get("api_key") or backup_api_key or ""
    m_base_url = entry_dict.get("base_url") or backup_base_url or ""

    if name and "@" not in name and m_factory:
        name = f"{name}@{m_factory}"

    return {
        "model": name,
        "factory": m_factory,
        "api_key": m_api_key,
        "base_url": m_base_url,
    }


# ---------------------------------------------------------------------------
# PEP 562 惰性 facade
# ---------------------------------------------------------------------------


def _base(key: str, default: Any = None) -> Any:
    """get_base_config 的 app_config 版（含 env 覆盖层；env 名回退语义一致）。"""
    raw = get_app_config().raw
    if key in raw:
        return raw[key]
    if default is None:
        return os.environ.get(key.upper())
    return default


def _doc_engine() -> str:
    return os.environ.get("DOC_ENGINE", "milvus").strip().lower()


def _storage_type() -> str:
    return os.getenv("STORAGE_IMPL", "MINIO")


def _engine_gated(section: str, engines: set[str], default: Any = None) -> Any:
    """镜像旧 init_settings 行为：仅当前选中的引擎，其配置 section 才非空。"""
    if _doc_engine() in engines:
        return _base(section, default if default is not None else {})
    return {}


def _storage_gated(section: str, types: set[str]) -> Any:
    # minio 的 password 解密已在 app_config 加载时完成（raw 即解密后值），
    # 此处不可再调 decrypt_database_config（启用加密的部署会二次解密损坏值）
    if _storage_type() not in types:
        return {}
    return _base(section, {})


def _resolved_cfg(kind: str) -> dict[str, str]:
    return get_app_config().user_default_llm.resolved_model(kind).as_dict()


def _embedding_mdl() -> str:
    model = get_app_config().user_default_llm.resolved_model("embedding_model").model
    if "tei-" in os.getenv("COMPOSE_PROFILES", ""):
        return os.getenv("TEI_MODEL", model or "Qwen/Qwen3-Embedding-0.6B")
    return model


def _register_enabled() -> int:
    try:
        return int(os.environ.get("REGISTER_ENABLED", "1"))
    except Exception:
        return 1


def _disable_password_login() -> bool:
    try:
        if os.environ.get("DISABLE_PASSWORD_LOGIN", "").lower() in ("1", "true", "yes"):
            return True
        return bool(get_app_config().authentication.disable_password_login)
    except Exception:
        return False


def _mail_default_sender() -> tuple:
    sender = get_app_config().smtp.mail_default_sender
    if sender and len(sender) >= 2:
        return (sender[0], sender[1])
    return ()


def _sandbox_host() -> str | None:
    if int(os.environ.get("SANDBOX_ENABLED", "0")):
        return os.environ.get("SANDBOX_HOST", "sandbox-executor-manager")
    return None


def _redis_conn() -> Any:
    from core.utils.redis_conn import REDIS_CONN

    return REDIS_CONN


def _lazy_class(module_name: str, attr: str) -> Any:
    return getattr(importlib.import_module(module_name), attr)


_INFINITY_DEFAULT = {"uri": "infinity:23817", "postgres_port": 5432, "db_name": "default_db"}

_LAZY: dict[str, Callable[[], Any]] = {
    # ---- 服务 ----
    "HOST_IP": lambda: get_app_config().multirag.host,
    "HOST_PORT": lambda: get_app_config().multirag.http_port,
    "ADMIN_REQUIRE_SUPERUSER": lambda: get_app_config().multirag.admin_require_superuser,
    "REGISTER_ENABLED": _register_enabled,
    "DISABLE_PASSWORD_LOGIN": _disable_password_login,
    # ---- 认证与 OAuth ----
    "CLIENT_AUTHENTICATION": lambda: get_app_config().authentication.client.get("switch", False),
    "HTTP_APP_KEY": lambda: get_app_config().authentication.client.get("http_app_key"),
    "GITHUB_OAUTH": lambda: get_app_config().oauth.get("github"),
    "FEISHU_OAUTH": lambda: get_app_config().oauth.get("feishu"),
    "OAUTH_CONFIG": lambda: get_app_config().oauth,
    # ---- LLM 默认模型 ----
    "LLM_FACTORY": lambda: get_app_config().user_default_llm.factory,
    "LLM_BASE_URL": lambda: get_app_config().user_default_llm.base_url,
    "API_KEY": lambda: get_app_config().user_default_llm.api_key,
    "ALLOWED_LLM_FACTORIES": lambda: get_app_config().user_default_llm.allowed_factories,
    "PARSERS": lambda: get_app_config().user_default_llm.parsers,
    "FACTORY_LLM_INFOS": get_factory_llm_infos,
    "CHAT_CFG": lambda: _resolved_cfg("chat_model"),
    "EMBEDDING_CFG": lambda: _resolved_cfg("embedding_model"),
    "RERANK_CFG": lambda: _resolved_cfg("rerank_model"),
    "ASR_CFG": lambda: _resolved_cfg("asr_model"),
    "IMAGE2TEXT_CFG": lambda: _resolved_cfg("image2text_model"),
    "CHAT_MDL": lambda: _resolved_cfg("chat_model")["model"],
    "EMBEDDING_MDL": _embedding_mdl,
    "RERANK_MDL": lambda: _resolved_cfg("rerank_model")["model"],
    "ASR_MDL": lambda: _resolved_cfg("asr_model")["model"],
    "IMAGE2TEXT_MDL": lambda: _resolved_cfg("image2text_model")["model"],
    # ---- 文档引擎（仅选中引擎的 section 非空，镜像旧行为）----
    "DOC_ENGINE": _doc_engine,
    "DOC_ENGINE_INFINITY": lambda: _doc_engine() == "infinity",
    "DOC_ENGINE_OCEANBASE": lambda: _doc_engine() == "oceanbase",
    "ES": lambda: _engine_gated("es", {"elasticsearch"}),
    "MILVUS": lambda: _engine_gated("milvus", {"milvus"}),
    "INFINITY": lambda: _engine_gated("infinity", {"infinity"}, _INFINITY_DEFAULT),
    "OS": lambda: _engine_gated("os", {"opensearch"}),
    "OB": lambda: _engine_gated("oceanbase", {"oceanbase"}) or _engine_gated("seekdb", {"seekdb"}),
    "VASTBASE": lambda: _engine_gated("vastbase", {"vastbase"}),
    # ---- 存储配置（仅选中类型的 section 非空，镜像旧行为）----
    "STORAGE_IMPL_TYPE": _storage_type,
    "AZURE": lambda: _storage_gated("azure", {"AZURE_SPN", "AZURE_SAS"}),
    "S3": lambda: _storage_gated("s3", {"AWS_S3"}),
    "MINIO": lambda: _storage_gated("minio", {"MINIO"}),
    "OSS": lambda: _storage_gated("oss", {"OSS"}),
    "GCS": lambda: _storage_gated("gcs", {"GCS"}),
    # ---- SMTP ----
    "SMTP_CONF": lambda: _base("smtp", {}),
    "MAIL_SERVER": lambda: get_app_config().smtp.mail_server,
    "MAIL_PORT": lambda: get_app_config().smtp.mail_port,
    "MAIL_USE_SSL": lambda: get_app_config().smtp.mail_use_ssl,
    "MAIL_USE_TLS": lambda: get_app_config().smtp.mail_use_tls,
    "MAIL_USERNAME": lambda: get_app_config().smtp.mail_username,
    "MAIL_PASSWORD": lambda: get_app_config().smtp.mail_password,
    "MAIL_DEFAULT_SENDER": _mail_default_sender,
    "MAIL_FRONTEND_URL": lambda: get_app_config().smtp.mail_frontend_url,
    # ---- 运行参数（env 随取随算）----
    "DOC_MAXIMUM_SIZE": lambda: int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024)),
    "DOC_BULK_SIZE": lambda: int(os.environ.get("DOC_BULK_SIZE", 4)),
    "EMBEDDING_BATCH_SIZE": lambda: int(os.environ.get("EMBEDDING_BATCH_SIZE", 16)),
    "SANDBOX_HOST": _sandbox_host,
    # ---- 资源句柄（委托 resources；核心资源未初始化会 fail-fast）----
    "SECRET_KEY": resources.secret_key,
    "docStoreConn": resources.doc_store,
    "msgStoreConn": resources.msg_store,
    "retriever": resources.retriever,
    "kg_retriever": resources.kg_retriever,
    "STORAGE_IMPL": resources.storage,
    "REDIS_CONN": _redis_conn,
    # ---- 存储实现类（懒 import，仅为保持 re-export 契约）----
    "MultiRAGMinio": lambda: _lazy_class("core.utils.minio_conn", "MultiRAGMinio"),
    "MultiRAGAzureSpnBlob": lambda: _lazy_class("core.utils.azure_spn_conn", "MultiRAGAzureSpnBlob"),
    "MultiRAGAzureSasBlob": lambda: _lazy_class("core.utils.azure_sas_conn", "MultiRAGAzureSasBlob"),
    "MultiRAGS3": lambda: _lazy_class("core.utils.s3_conn", "MultiRAGS3"),
    "MultiRAGOSS": lambda: _lazy_class("core.utils.oss_conn", "MultiRAGOSS"),
    "MultiRAGGCS": lambda: _lazy_class("core.utils.gcs_conn", "MultiRAGGCS"),
    "OpenDALStorage": lambda: _lazy_class("core.utils.opendal_conn", "OpenDALStorage"),
    # ---- 杂项服务配置 ----
    "AIFORBI_BASE_CONFIG": lambda: _base("aiforbi", {}),
    "AIFORBI_BASE_URL": lambda: _base("aiforbi", {}).get("base_url"),
    "AIFORBI_API_KEY": lambda: _base("aiforbi", {}).get("api_key"),
    "AIFORBI_MODEL_ID": lambda: _base("aiforbi", {}).get("model_id"),
    "AI_TRANSLATE_BASE_CONFIG": lambda: _base("ai_translate", {}),
    "AI_TRANSLATE_BASE_URL": lambda: _base("ai_translate", {}).get("base_url"),
    "AI_TRANSLATE_API_KEY": lambda: _base("ai_translate", {}).get("api_key"),
    "AI_TRANSLATE_MODEL_ID": lambda: _base("ai_translate", {}).get("model_id"),
    "SCRIPT_SCHEDULER_BASE_CONFIG": lambda: _base("script_scheduler", {}),
    "SCRIPT_SCHEDULER_HOST": lambda: _base("script_scheduler", {}).get("host"),
    "SCRIPT_SCHEDULER_PORT": lambda: _base("script_scheduler", {}).get("port"),
    "DCS_SERVER_BASE_CONFIG": lambda: _base("dcs_server", {}),
    "DCS_SERVER_PROTOCOL": lambda: _base("dcs_server", {}).get("protocol"),
    "DCS_SERVER_HOST": lambda: _base("dcs_server", {}).get("host"),
    "DCS_SERVER_PORT": lambda: _base("dcs_server", {}).get("port"),
    "DCS_SEMANTIC_SERVER_CONFIG": lambda: _base("dcs_server", {}).get("semantic_server", {}),
    "DCS_SEMANTIC_SERVER_ACCESS_KEY": lambda: _base("dcs_server", {}).get("semantic_server", {}).get("access_key"),
    "DCS_SEMANTIC_SERVER_SECRET_KEY": lambda: _base("dcs_server", {}).get("semantic_server", {}).get("secret_key"),
}


def __getattr__(name: str) -> Any:
    try:
        factory = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'common.settings' has no attribute {name!r}") from None
    return factory()


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


def init_settings():
    """兼容别名（上游移植的入口文件继续调用本函数，零 diff）。

    等价 ``common.bootstrap.ensure_initialized(force=True)``：加载配置 +
    （重）建资源，保持旧"每次调用都重建"的语义。
    """
    bootstrap.ensure_initialized(force=True)


def check_and_install_torch():
    global PARALLEL_DEVICES
    try:
        pip_install_torch()
        import torch.cuda

        PARALLEL_DEVICES = torch.cuda.device_count()
        logging.info(f"found {PARALLEL_DEVICES} gpus")
    except Exception:
        logging.info("can't import package 'torch'")


def print_rag_settings():
    logging.info(f"MAX_CONTENT_LENGTH: {__getattr__('DOC_MAXIMUM_SIZE')}")
    logging.info(f"MAX_FILE_COUNT_PER_USER: {int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))}")
