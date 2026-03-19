import os
import json
import secrets
import logging
from datetime import date

from common.constants import MULTI_RAG_SERVICE_NAME, SVR_QUEUE_NAME, Storage
from common.file_utils import get_project_base_directory
from common.config_utils import get_base_config, decrypt_database_config
from common.misc_utils import pip_install_torch
import core.utils
import core.utils.milvus_conn
import core.utils.es_conn
import core.utils.infinity_conn
import core.utils.ob_conn
import core.utils.opensearch_conn
import core.utils.vastbase_conn
from core.utils.azure_sas_conn import MultiRAGAzureSasBlob
from core.utils.azure_spn_conn import MultiRAGAzureSpnBlob
from core.utils.gcs_conn import MultiRAGGCS
from core.utils.minio_conn import MultiRAGMinio
from core.utils.opendal_conn import OpenDALStorage
from core.utils.s3_conn import MultiRAGS3
from core.utils.oss_conn import MultiRAGOSS
from core.nlp import search
import memory.utils.es_conn as memory_es_conn
import memory.utils.infinity_conn as memory_infinity_conn
import memory.utils.milvus_conn as memory_milvus_conn
import memory.utils.ob_conn as memory_ob_conn
# import memory.utils.vastbase_conn as memory_vastbase_conn

# Lighten mode
LIGHTEN = int(os.environ.get("LIGHTEN", "0"))

# LLM settings
LLM = None
LLM_FACTORY = None
LLM_BASE_URL = None
CHAT_MDL = ""
EMBEDDING_MDL = ""
RERANK_MDL = ""
ASR_MDL = ""
IMAGE2TEXT_MDL = ""

CHAT_CFG = ""
EMBEDDING_CFG = ""
RERANK_CFG = ""
ASR_CFG = ""
IMAGE2TEXT_CFG = ""
API_KEY = None
PARSERS = None
HOST_IP = None
HOST_PORT = None
SECRET_KEY = None
FACTORY_LLM_INFOS = None
ALLOWED_LLM_FACTORIES = None
ADMIN_REQUIRE_SUPERUSER = None

# Authentication
AUTHENTICATION_CONF = None
CLIENT_AUTHENTICATION = None
HTTP_APP_KEY = None
GITHUB_OAUTH = None
FEISHU_OAUTH = None
OAUTH_CONFIG = None

# Doc engine settings
DOC_ENGINE = os.getenv('DOC_ENGINE', 'milvus')
DOC_ENGINE_INFINITY = (DOC_ENGINE.lower() == "infinity")
DOC_ENGINE_OCEANBASE = (DOC_ENGINE.lower() == "oceanbase")
docStoreConn = None
msgStoreConn = None
retriever = None
kg_retriever = None

# User registration switch
REGISTER_ENABLED = 1

# Sandbox settings
SANDBOX_HOST = None
STRONG_TEST_COUNT = int(os.environ.get("STRONG_TEST_COUNT", "8"))

# Built-in embedding models
BUILTIN_EMBEDDING_MODELS = ["BAAI/bge-large-zh-v1.5@BAAI", "maidalun1020/bce-embedding-base_v1@Youdao"]

# SMTP settings
SMTP_CONF = None
MAIL_SERVER = ""
MAIL_PORT = 000
MAIL_USE_SSL = True
MAIL_USE_TLS = False
MAIL_USERNAME = ""
MAIL_PASSWORD = ""
MAIL_DEFAULT_SENDER = ()
MAIL_FRONTEND_URL = ""

# Storage and database configurations
ES = {}
MILVUS = {}
VASTBASE = {}
INFINITY = {}
AZURE = {}
S3 = {}
MINIO = {}
OB = {}
OSS = {}
OS = {}
GCS = {}

# Core settings (from core/settings.py)
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")
DOC_MAXIMUM_SIZE: int = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))
DOC_BULK_SIZE: int = int(os.environ.get("DOC_BULK_SIZE", 4))
EMBEDDING_BATCH_SIZE: int = int(os.environ.get("EMBEDDING_BATCH_SIZE", 16))
PARALLEL_DEVICES: int = 0

# Storage factory settings
STORAGE_IMPL_TYPE = os.getenv('STORAGE_IMPL', 'MINIO')
STORAGE_IMPL = None


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
    secret_key = os.environ.get("MULTIRAG_SECRET_KEY")
    if secret_key and len(secret_key) >= 32:
        return secret_key

    # Check if there's a configured secret key
    configured_key = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("secret_key")
    if configured_key and configured_key != str(date.today()) and len(configured_key) >= 32:
        return configured_key

    # Generate a new secure key and warn about it
    new_key = secrets.token_hex(32)
    # logging.warning(f"SECURITY WARNING: Using auto-generated SECRET_KEY. Generated key: {new_key}")
    logging.warning(f"SECURITY WARNING: Using auto-generated SECRET_KEY.")
    return new_key


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


def init_settings():
    global ALLOWED_LLM_FACTORIES, LLM_FACTORY, LLM_BASE_URL
    llm_settings = get_base_config("user_default_llm", {}) or {}
    llm_default_models = llm_settings.get("default_models", {}) or {}
    LLM_FACTORY = llm_settings.get("factory", "") or ""
    LLM_BASE_URL = llm_settings.get("base_url", "") or ""
    ALLOWED_LLM_FACTORIES = llm_settings.get("allowed_factories", None)

    global REGISTER_ENABLED
    try:
        REGISTER_ENABLED = int(os.environ.get("REGISTER_ENABLED", "1"))
    except Exception:
        pass

    global FACTORY_LLM_INFOS
    try:
        with open(os.path.join(get_project_base_directory(), "configs", "llm_factories.json"), "r") as f:
            FACTORY_LLM_INFOS = json.load(f)["factory_llm_infos"]
    except Exception:
        FACTORY_LLM_INFOS = []

    global API_KEY
    API_KEY = llm_settings.get("api_key")

    global PARSERS
    PARSERS = llm_settings.get(
        "parsers", "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag"
    )

    global CHAT_MDL, EMBEDDING_MDL, RERANK_MDL, ASR_MDL, IMAGE2TEXT_MDL
    chat_entry = _parse_model_entry(llm_default_models.get("chat_model", CHAT_MDL))
    embedding_entry = _parse_model_entry(llm_default_models.get("embedding_model", EMBEDDING_MDL))
    rerank_entry = _parse_model_entry(llm_default_models.get("rerank_model", RERANK_MDL))
    asr_entry = _parse_model_entry(llm_default_models.get("asr_model", ASR_MDL))
    image2text_entry = _parse_model_entry(llm_default_models.get("image2text_model", IMAGE2TEXT_MDL))

    global CHAT_CFG, EMBEDDING_CFG, RERANK_CFG, ASR_CFG, IMAGE2TEXT_CFG
    CHAT_CFG = _resolve_per_model_config(chat_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    EMBEDDING_CFG = _resolve_per_model_config(embedding_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    RERANK_CFG = _resolve_per_model_config(rerank_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    ASR_CFG = _resolve_per_model_config(asr_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    IMAGE2TEXT_CFG = _resolve_per_model_config(image2text_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)

    CHAT_MDL = CHAT_CFG.get("model", "") or ""
    EMBEDDING_MDL = EMBEDDING_CFG.get("model", "") or ""
    compose_profiles = os.getenv("COMPOSE_PROFILES", "")
    if "tei-" in compose_profiles:
        EMBEDDING_MDL = os.getenv("TEI_MODEL", EMBEDDING_MDL or "Qwen/Qwen3-Embedding-0.6B")
    RERANK_MDL = RERANK_CFG.get("model", "") or ""
    ASR_MDL = ASR_CFG.get("model", "") or ""
    IMAGE2TEXT_MDL = IMAGE2TEXT_CFG.get("model", "") or ""

    global HOST_IP, HOST_PORT
    HOST_IP = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("host", "127.0.0.1")
    HOST_PORT = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("http_port")

    global SECRET_KEY
    SECRET_KEY = _get_or_create_secret_key()
    # SECRET_KEY = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("secret_key", str(date.today()))  # 老版本密钥生成方案

    # Admin permission configuration
    global ADMIN_REQUIRE_SUPERUSER
    ADMIN_REQUIRE_SUPERUSER = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("admin_require_superuser", False)

    # authentication
    authentication_conf = get_base_config("authentication", {})

    global CLIENT_AUTHENTICATION, HTTP_APP_KEY, GITHUB_OAUTH, FEISHU_OAUTH, OAUTH_CONFIG
    # client
    CLIENT_AUTHENTICATION = authentication_conf.get("client", {}).get("switch", False)
    HTTP_APP_KEY = authentication_conf.get("client", {}).get("http_app_key")
    GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
    FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")
    OAUTH_CONFIG = get_base_config("oauth", {})

    global DOC_ENGINE, DOC_ENGINE_INFINITY, DOC_ENGINE_OCEANBASE, docStoreConn, ES, OB, OS, INFINITY, MILVUS, VASTBASE
    DOC_ENGINE = os.environ.get("DOC_ENGINE", "milvus").strip()
    DOC_ENGINE_INFINITY = (DOC_ENGINE.lower() == "infinity")
    DOC_ENGINE_OCEANBASE = (DOC_ENGINE.lower() == "oceanbase")
    lower_case_doc_engine = DOC_ENGINE.lower()

    if lower_case_doc_engine == "elasticsearch":
        ES = get_base_config("es", {})
        docStoreConn = core.utils.es_conn.ESConnection()
    elif lower_case_doc_engine == "milvus":
        MILVUS = get_base_config("milvus", {})
        docStoreConn = core.utils.milvus_conn.MilvusConnection()
    elif lower_case_doc_engine == "infinity":
        INFINITY = get_base_config("infinity", {"uri": "infinity:23817", "postgres_port": 5432, "db_name": "default_db"})
        docStoreConn = core.utils.infinity_conn.InfinityConnection()
    elif lower_case_doc_engine == "opensearch":
        OS = get_base_config("os", {})
        docStoreConn = core.utils.opensearch_conn.OSConnection()
    elif lower_case_doc_engine == "oceanbase":
        OB = get_base_config("oceanbase", {})
        docStoreConn = core.utils.ob_conn.OBConnection()
    elif lower_case_doc_engine == "seekdb":
        OB = get_base_config("seekdb", {})
        docStoreConn = core.utils.ob_conn.OBConnection()
    elif lower_case_doc_engine == "vastbase":
        VASTBASE = get_base_config("vastbase", {})
        docStoreConn = core.utils.vastbase_conn.VastBaseConnection()
    else:
        raise Exception(f"Not supported doc engine: {DOC_ENGINE}")

    global msgStoreConn
    # use the same engine for message store
    if lower_case_doc_engine == "elasticsearch":
        msgStoreConn = memory_es_conn.ESConnection()
    elif lower_case_doc_engine == "milvus":
        msgStoreConn = memory_milvus_conn.MilvusConnection()
    elif lower_case_doc_engine == "infinity":
        INFINITY = get_base_config("infinity", {
            "uri": "infinity:23817",
            "postgres_port": 5432,
            "db_name": "default_db"
        })
        msgStoreConn = memory_infinity_conn.InfinityConnection()
    elif lower_case_doc_engine in ["oceanbase", "seekdb"]:
        msgStoreConn = memory_ob_conn.OBConnection()
    # elif lower_case_doc_engine == "vastbase":
    #     VASTBASE = get_base_config("vastbase", {})
    #     docStoreConn = memory_vastbase_conn.VastBaseConnection()

    global AZURE, S3, MINIO, OSS, GCS
    if STORAGE_IMPL_TYPE in ['AZURE_SPN', 'AZURE_SAS']:
        AZURE = get_base_config("azure", {})
    elif STORAGE_IMPL_TYPE == 'AWS_S3':
        S3 = get_base_config("s3", {})
    elif STORAGE_IMPL_TYPE == 'MINIO':
        MINIO = decrypt_database_config(name="minio")
    elif STORAGE_IMPL_TYPE == 'OSS':
        OSS = get_base_config("oss", {})
    elif STORAGE_IMPL_TYPE == 'GCS':
        GCS = get_base_config("gcs", {})

    global STORAGE_IMPL
    storage_impl = StorageFactory.create(Storage[STORAGE_IMPL_TYPE])

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
    if int(os.environ.get("SANDBOX_ENABLED", "0")):
        SANDBOX_HOST = os.environ.get("SANDBOX_HOST", "sandbox-executor-manager")

    global SMTP_CONF
    SMTP_CONF = get_base_config("smtp", {})

    global MAIL_SERVER, MAIL_PORT, MAIL_USE_SSL, MAIL_USE_TLS, MAIL_USERNAME, MAIL_PASSWORD, MAIL_DEFAULT_SENDER, MAIL_FRONTEND_URL
    MAIL_SERVER = SMTP_CONF.get("mail_server", "")
    MAIL_PORT = SMTP_CONF.get("mail_port", 000)
    MAIL_USE_SSL = SMTP_CONF.get("mail_use_ssl", True)
    MAIL_USE_TLS = SMTP_CONF.get("mail_use_tls", False)
    MAIL_USERNAME = SMTP_CONF.get("mail_username", "")
    MAIL_PASSWORD = SMTP_CONF.get("mail_password", "")
    mail_default_sender = SMTP_CONF.get("mail_default_sender", [])
    if mail_default_sender and len(mail_default_sender) >= 2:
        MAIL_DEFAULT_SENDER = (mail_default_sender[0], mail_default_sender[1])
    MAIL_FRONTEND_URL = SMTP_CONF.get("mail_frontend_url", "")

    global DOC_MAXIMUM_SIZE, DOC_BULK_SIZE, EMBEDDING_BATCH_SIZE
    DOC_MAXIMUM_SIZE = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))
    DOC_BULK_SIZE = int(os.environ.get("DOC_BULK_SIZE", 4))
    EMBEDDING_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", 16))

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
    logging.info(f"MAX_CONTENT_LENGTH: {DOC_MAXIMUM_SIZE}")
    logging.info(f"MAX_FILE_COUNT_PER_USER: {int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))}")


# AIFORBI configurations (from api/settings.py)
AIFORBI_BASE_CONFIG = get_base_config("aiforbi", {})
AIFORBI_BASE_URL = AIFORBI_BASE_CONFIG.get("base_url")
AIFORBI_API_KEY = AIFORBI_BASE_CONFIG.get("api_key")
AIFORBI_MODEL_ID = AIFORBI_BASE_CONFIG.get("model_id")

AI_TRANSLATE_BASE_CONFIG = get_base_config("ai_translate", {})
AI_TRANSLATE_BASE_URL = AI_TRANSLATE_BASE_CONFIG.get("base_url")
AI_TRANSLATE_API_KEY = AI_TRANSLATE_BASE_CONFIG.get("api_key")
AI_TRANSLATE_MODEL_ID = AI_TRANSLATE_BASE_CONFIG.get("model_id")

SCRIPT_SCHEDULER_BASE_CONFIG = get_base_config("script_scheduler", {})
SCRIPT_SCHEDULER_HOST = SCRIPT_SCHEDULER_BASE_CONFIG.get("host")
SCRIPT_SCHEDULER_PORT = SCRIPT_SCHEDULER_BASE_CONFIG.get("port")

DCS_SERVER_BASE_CONFIG = get_base_config("dcs_server", {})
DCS_SERVER_PROTOCOL = DCS_SERVER_BASE_CONFIG.get("protocol")
DCS_SERVER_HOST = DCS_SERVER_BASE_CONFIG.get("host")
DCS_SERVER_PORT = DCS_SERVER_BASE_CONFIG.get("port")

DCS_SEMANTIC_SERVER_CONFIG = DCS_SERVER_BASE_CONFIG.get("semantic_server", {})
DCS_SEMANTIC_SERVER_ACCESS_KEY = DCS_SEMANTIC_SERVER_CONFIG.get("access_key")
DCS_SEMANTIC_SERVER_SECRET_KEY = DCS_SEMANTIC_SERVER_CONFIG.get("secret_key")