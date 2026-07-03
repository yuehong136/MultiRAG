"""全局配置兼容 facade（配置重构 Phase 2，方案见 internal/config_bootstrap_refactor_plan.md）。

本模块是 **上游兼容面**：610+ 存量调用点与 ragflow 上游移植 diff 继续使用
``settings.X`` 访问方式，值由 PEP 562 模块 ``__getattr__`` 惰性委托给
:mod:`common.app_config`（类型化配置，不可变）。

- 纯配置项（HOST_IP、MAIL_*、CHAT_CFG…）：**无需初始化**，随取随算——
  彻底消灭"import 顺序决定配置是否可用"的事故类别；
- 资源句柄（docStoreConn、STORAGE_IMPL、retriever、SECRET_KEY…）：仍由
  :func:`init_settings` 创建（Phase 3 迁往 common/resources.py + bootstrap）；
- 公开名字清单受 tests/unit/test_settings_facade_inventory.py 契约保护，
  移植映射见 internal/ragflow_settings_porting_map.md。

注意：对本模块 ``settings.X = value`` 的写入（含 monkeypatch）会在模块 dict
写入真实属性、遮蔽同名惰性值——配置不可变，因此语义安全；生产代码禁止写入。
"""

import logging
import os
import secrets
from collections.abc import Callable
from typing import Any

import core.utils
import core.utils.es_conn
import core.utils.infinity_conn
import core.utils.milvus_conn
import core.utils.ob_conn
import core.utils.opensearch_conn
import core.utils.vastbase_conn
import memory.utils.es_conn as memory_es_conn
import memory.utils.infinity_conn as memory_infinity_conn
import memory.utils.milvus_conn as memory_milvus_conn
import memory.utils.ob_conn as memory_ob_conn
from common.app_config import get_app_config, get_factory_llm_infos

# 纯 re-export（facade 公开契约的一部分，受 inventory 测试保护）：
# 冗余别名写法向 ruff 声明这是有意导出，勿清理
from common.config_utils import decrypt_database_config as decrypt_database_config
from common.config_utils import get_base_config as get_base_config
from common.constants import MULTI_RAG_SERVICE_NAME as MULTI_RAG_SERVICE_NAME
from common.constants import SVR_QUEUE_NAME, Storage
from common.file_utils import get_project_base_directory
from common.misc_utils import pip_install_torch
from core.nlp import search
from core.utils.azure_sas_conn import MultiRAGAzureSasBlob
from core.utils.azure_spn_conn import MultiRAGAzureSpnBlob
from core.utils.gcs_conn import MultiRAGGCS
from core.utils.minio_conn import MultiRAGMinio
from core.utils.opendal_conn import OpenDALStorage
from core.utils.oss_conn import MultiRAGOSS
from core.utils.redis_conn import REDIS_CONN
from core.utils.s3_conn import MultiRAGS3

# ---------------------------------------------------------------------------
# import 期冻结的环境常量（与历史行为一致）
# ---------------------------------------------------------------------------

LIGHTEN = int(os.environ.get("LIGHTEN", "0"))
STRONG_TEST_COUNT = int(os.environ.get("STRONG_TEST_COUNT", "8"))
BUILTIN_EMBEDDING_MODELS = ["BAAI/bge-large-zh-v1.5@BAAI", "maidalun1020/bce-embedding-base_v1@Youdao"]
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")

# ---------------------------------------------------------------------------
# 资源句柄与运行时状态（由 init_settings 创建；Phase 3 迁往 common/resources.py）
# ---------------------------------------------------------------------------

LLM = None  # 历史遗留，无消费者
AUTHENTICATION_CONF = None  # 历史遗留：init_settings 从未赋值，钉板保持 None
SECRET_KEY = None
docStoreConn: Any = None
msgStoreConn = None
retriever = None
kg_retriever = None
STORAGE_IMPL: Any = None
PARALLEL_DEVICES: int = 0
SANDBOX_HOST = None  # init_settings 按 SANDBOX_ENABLED 环境变量填充


class StorageFactory:
    storage_mapping = {
        Storage.MINIO: MultiRAGMinio,
        Storage.AZURE_SPN: MultiRAGAzureSpnBlob,
        Storage.AZURE_SAS: MultiRAGAzureSasBlob,
        Storage.AWS_S3: MultiRAGS3,
        Storage.OSS: MultiRAGOSS,
        Storage.OPENDAL: OpenDALStorage,
        Storage.GCS: MultiRAGGCS,
    }

    @classmethod
    def create(cls, storage: Storage):
        return cls.storage_mapping[storage]()


def get_svr_queue_name(priority: int) -> str:
    if priority == 0:
        return SVR_QUEUE_NAME
    return f"{SVR_QUEUE_NAME}_{priority}"


def get_svr_queue_names():
    return [get_svr_queue_name(priority) for priority in [1, 0]]


def _get_or_create_secret_key():
    # Generate a new secure key and warn about it
    generated_key = secrets.token_hex(32)
    secret_key = REDIS_CONN.get_or_create_secret_key("multirag:system:secret_key", generated_key)
    logging.warning("SECURITY WARNING: Using auto-generated SECRET_KEY.")
    return secret_key


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
# PEP 562 惰性 facade：纯配置项随取随算，无需任何初始化
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
    # ---- 存储（仅选中类型的 section 非空，镜像旧行为）----
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


# ---------------------------------------------------------------------------
# 资源初始化（Phase 3 将整体迁往 common/resources.py + common/bootstrap.py）
# ---------------------------------------------------------------------------


def init_settings():
    """初始化有状态资源。

    纯配置项已由模块 facade 惰性提供、无需初始化；本函数只负责：
    SECRET_KEY（Redis）、doc/msg store 连接、存储实现、检索器。幂等安全（重复
    调用会重建资源，与历史行为一致）。
    """
    global SECRET_KEY
    SECRET_KEY = _get_or_create_secret_key()

    engine = _doc_engine()

    global docStoreConn
    if engine == "elasticsearch":
        docStoreConn = core.utils.es_conn.ESConnection()
    elif engine == "milvus":
        docStoreConn = core.utils.milvus_conn.MilvusConnection()
    elif engine == "infinity":
        docStoreConn = core.utils.infinity_conn.InfinityConnection()
    elif engine == "opensearch":
        docStoreConn = core.utils.opensearch_conn.OSConnection()
    elif engine == "oceanbase":
        docStoreConn = core.utils.ob_conn.OBConnection()
    elif engine == "seekdb":
        docStoreConn = core.utils.ob_conn.OBConnection()
    elif engine == "vastbase":
        docStoreConn = core.utils.vastbase_conn.VastBaseConnection()
    else:
        raise Exception(f"Not supported doc engine: {engine}")

    global msgStoreConn
    # use the same engine for message store
    if engine == "elasticsearch":
        msgStoreConn = memory_es_conn.ESConnection()
    elif engine == "milvus":
        msgStoreConn = memory_milvus_conn.MilvusConnection()
    elif engine == "infinity":
        msgStoreConn = memory_infinity_conn.InfinityConnection()
    elif engine in ["oceanbase", "seekdb"]:
        msgStoreConn = memory_ob_conn.OBConnection()

    global STORAGE_IMPL
    storage_impl = StorageFactory.create(Storage[_storage_type()])

    # Define crypto settings
    crypto_enabled = os.environ.get("MultiRAG_CRYPTO_ENABLED", "false").lower() == "true"

    # Check if encryption is enabled
    if crypto_enabled:
        try:
            from core.utils.encrypted_storage import create_encrypted_storage

            algorithm = os.environ.get("MultiRAG_CRYPTO_ALGORITHM", "aes-256-cbc")
            crypto_key = os.environ.get("MultiRAG_CRYPTO_KEY")

            STORAGE_IMPL = create_encrypted_storage(storage_impl, algorithm=algorithm, key=crypto_key, encryption_enabled=crypto_enabled)
        except Exception as e:
            logging.error(f"Failed to initialize encrypted storage: {e}")
            STORAGE_IMPL = storage_impl
    else:
        STORAGE_IMPL = storage_impl

    global retriever, kg_retriever
    retriever = search.Dealer(docStoreConn)
    from core.graphrag import search as kg_search

    kg_retriever = kg_search.KGSearch(docStoreConn)

    global SANDBOX_HOST
    SANDBOX_HOST = _sandbox_host()

    os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"


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
