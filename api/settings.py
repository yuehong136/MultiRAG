import json
import os
import secrets
from datetime import date
from enum import IntEnum, Enum

import core.utils
import core.utils.milvus_conn
# import core.utils.infinity_conn, opensearch_conn
from api.constants import MULTI_RAG_SERVICE_NAME
from api.utils.configs import get_base_config
from api.utils.file_utils import get_project_base_directory
from core.nlp import search

LIGHTEN = int(os.environ.get("LIGHTEN", "0"))

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
ADMIN_REQUIRE_SUPERUSER = None

# DATABASE_TYPE = os.getenv("DB_TYPE", "postgresql")
# DATABASE = decrypt_database_config(name=DATABASE_TYPE)

# authentication
AUTHENTICATION_CONF = None

# client
CLIENT_AUTHENTICATION = None
HTTP_APP_KEY = None
GITHUB_OAUTH = None
FEISHU_OAUTH = None
OAUTH_CONFIG = None
DOC_ENGINE = None
docStoreConn = None

retriever = None
kg_retriever = None

# user registration switch
REGISTER_ENABLED = 1


# sandbox-executor-manager
SANDBOX_ENABLED = 0
SANDBOX_HOST = None
STRONG_TEST_COUNT = int(os.environ.get("STRONG_TEST_COUNT", "8"))

BUILTIN_EMBEDDING_MODELS = ["BAAI/bge-large-zh-v1.5@BAAI", "maidalun1020/bce-embedding-base_v1@Youdao"]

SMTP_CONF = None
MAIL_SERVER = ""
MAIL_PORT = 000
MAIL_USE_SSL= True
MAIL_USE_TLS = False
MAIL_USERNAME = ""
MAIL_PASSWORD = ""
MAIL_DEFAULT_SENDER = ()
MAIL_FRONTEND_URL = ""


def get_or_create_secret_key():
    secret_key = os.environ.get("MULTI_RAG_SERVICE_NAME")
    if secret_key and len(secret_key) >= 32:
        return secret_key

    # Check if there's a configured secret key
    configured_key = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("secret_key")
    if configured_key and configured_key != str(date.today()) and len(configured_key) >= 32:
        return configured_key

    # Generate a new secure key and warn about it
    import logging

    new_key = secrets.token_hex(32)
    logging.warning(f"SECURITY WARNING: Using auto-generated SECRET_KEY. Generated key: {new_key}")
    return new_key


def init_settings():
    # global LLM, LLM_FACTORY, LLM_BASE_URL, LIGHTEN, DATABASE_TYPE, DATABASE, FACTORY_LLM_INFOS, REGISTER_ENABLED
    # LIGHTEN = int(os.environ.get('LIGHTEN', "0"))
    # DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
    # DATABASE = decrypt_database_config(name=DATABASE_TYPE)
    # LLM = get_base_config("user_default_llm", {})
    # LLM_FACTORY = LLM.get("factory", "ZHIPU-AI")
    # LLM_BASE_URL = LLM.get("base_url")
    global LLM, LLM_FACTORY, LLM_BASE_URL, LIGHTEN, FACTORY_LLM_INFOS, REGISTER_ENABLED
    LIGHTEN = int(os.environ.get('LIGHTEN', "0"))
    LLM = get_base_config("user_default_llm", {}) or {}
    LLM_DEFAULT_MODELS = LLM.get("default_models", {}) or {}
    LLM_FACTORY = LLM.get("factory", "") or ""
    LLM_BASE_URL = LLM.get("base_url", "") or ""
    try:
        REGISTER_ENABLED = int(os.environ.get("REGISTER_ENABLED", "1"))
    except Exception:
        pass

    try:
        with open(os.path.join(get_project_base_directory(), "configs", "llm_factories.json"), "r") as f:
            FACTORY_LLM_INFOS = json.load(f)["factory_llm_infos"]
    except Exception:
        FACTORY_LLM_INFOS = []

    global CHAT_MDL, EMBEDDING_MDL, RERANK_MDL, ASR_MDL, IMAGE2TEXT_MDL
    global CHAT_CFG, EMBEDDING_CFG, RERANK_CFG, ASR_CFG, IMAGE2TEXT_CFG
    if not LIGHTEN:
        EMBEDDING_MDL = BUILTIN_EMBEDDING_MODELS[0]

    if LLM_DEFAULT_MODELS:
        CHAT_MDL = LLM_DEFAULT_MODELS.get("chat_model", CHAT_MDL)
        EMBEDDING_MDL = LLM_DEFAULT_MODELS.get("embedding_model", EMBEDDING_MDL)
        RERANK_MDL = LLM_DEFAULT_MODELS.get("rerank_model", RERANK_MDL)
        ASR_MDL = LLM_DEFAULT_MODELS.get("asr_model", ASR_MDL)
        IMAGE2TEXT_MDL = LLM_DEFAULT_MODELS.get("image2text_model", IMAGE2TEXT_MDL)

    global API_KEY, PARSERS, HOST_IP, HOST_PORT, SECRET_KEY
    API_KEY = LLM.get("api_key", "")
    PARSERS = LLM.get(
        "parsers", "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag"
    )

    chat_entry = _parse_model_entry(LLM_DEFAULT_MODELS.get("chat_model", CHAT_MDL))
    embedding_entry = _parse_model_entry(LLM_DEFAULT_MODELS.get("embedding_model", EMBEDDING_MDL))
    rerank_entry = _parse_model_entry(LLM_DEFAULT_MODELS.get("rerank_model", RERANK_MDL))
    asr_entry = _parse_model_entry(LLM_DEFAULT_MODELS.get("asr_model", ASR_MDL))
    image2text_entry = _parse_model_entry(LLM_DEFAULT_MODELS.get("image2text_model", IMAGE2TEXT_MDL))

    CHAT_CFG = _resolve_per_model_config(chat_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    EMBEDDING_CFG = _resolve_per_model_config(embedding_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    RERANK_CFG = _resolve_per_model_config(rerank_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    ASR_CFG = _resolve_per_model_config(asr_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)
    IMAGE2TEXT_CFG = _resolve_per_model_config(image2text_entry, LLM_FACTORY, API_KEY, LLM_BASE_URL)

    CHAT_MDL = CHAT_CFG.get("model", "") or ""
    EMBEDDING_MDL = EMBEDDING_CFG.get("model", "") or ""
    RERANK_MDL = RERANK_CFG.get("model", "") or ""
    ASR_MDL = ASR_CFG.get("model", "") or ""
    IMAGE2TEXT_MDL = IMAGE2TEXT_CFG.get("model", "") or ""

    HOST_IP = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("host", "127.0.0.1")
    HOST_PORT = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("http_port")

    # SECRET_KEY = get_or_create_secret_key()
    SECRET_KEY = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("secret_key", str(date.today()))
    
    # 管理后台权限配置
    global ADMIN_REQUIRE_SUPERUSER
    ADMIN_REQUIRE_SUPERUSER = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("admin_require_superuser", False)

    global AUTHENTICATION_CONF, CLIENT_AUTHENTICATION, HTTP_APP_KEY, GITHUB_OAUTH, FEISHU_OAUTH, OAUTH_CONFIG
    # authentication
    AUTHENTICATION_CONF = get_base_config("authentication", {})

    # client
    CLIENT_AUTHENTICATION = AUTHENTICATION_CONF.get("client", {}).get("switch", False)
    HTTP_APP_KEY = AUTHENTICATION_CONF.get("client", {}).get("http_app_key")
    GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
    FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")

    OAUTH_CONFIG = get_base_config("oauth", {})

    global DOC_ENGINE, docStoreConn, retriever, kg_retriever
    DOC_ENGINE = os.environ.get("DOC_ENGINE", "milvus")
    # DOC_ENGINE = os.environ.get('DOC_ENGINE', "opensearch")
    lower_case_doc_engine = DOC_ENGINE.lower()
    if lower_case_doc_engine == "milvus":
        docStoreConn = core.utils.milvus_conn.MilvusConnection()
    elif lower_case_doc_engine == "opensearch":
        docStoreConn = core.utils.opensearch_conn.OSConnection()
    elif lower_case_doc_engine == "infinity":
        docStoreConn = core.utils.infinity_conn.InfinityConnection()
    else:
        raise Exception(f"Not supported doc engine: {DOC_ENGINE}")

    retriever = search.Dealer(docStoreConn)
    from graphrag import search as kg_search

    kg_retriever = kg_search.KGSearch(docStoreConn)

    if int(os.environ.get("SANDBOX_ENABLED", "0")):
        global SANDBOX_HOST
        SANDBOX_HOST = os.environ.get("SANDBOX_HOST", "sandbox-executor-manager")

    global SMTP_CONF, MAIL_SERVER, MAIL_PORT, MAIL_USE_SSL, MAIL_USE_TLS
    global MAIL_USERNAME, MAIL_PASSWORD, MAIL_DEFAULT_SENDER, MAIL_FRONTEND_URL
    SMTP_CONF = get_base_config("smtp", {})

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


class CustomEnum(Enum):
    @classmethod
    def valid(cls, value):
        try:
            cls(value)
            return True
        except BaseException:
            return False

    @classmethod
    def values(cls):
        return [member.value for member in cls.__members__.values()]

    @classmethod
    def names(cls):
        return [member.name for member in cls.__members__.values()]


class RetCode(IntEnum, CustomEnum):
    SUCCESS = 0
    NOT_EFFECTIVE = 10
    EXCEPTION_ERROR = 100
    ARGUMENT_ERROR = 101
    DATA_ERROR = 102
    OPERATING_ERROR = 103
    CONNECTION_ERROR = 105
    RUNNING = 106
    PERMISSION_ERROR = 108
    AUTHENTICATION_ERROR = 109
    UNAUTHORIZED = 401
    SERVER_ERROR = 500
    FORBIDDEN = 403
    NOT_FOUND = 404


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


# AIFORBI
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