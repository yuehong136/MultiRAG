import os
from datetime import date
from enum import IntEnum, Enum
import core.utils
from core.nlp import search
from graphrag import search as kg_search
from core.utils.milvus_conn import MILVUS_CONNECTION
from api.utils import get_base_config, decrypt_database_config
from api.constants import MULTI_RAG_SERVICE_NAME

LIGHTEN = int(os.environ.get('LIGHTEN', "0"))

LLM = None
LLM_FACTORY = None
LLM_BASE_URL = None
CHAT_MDL = ""
EMBEDDING_MDL = ""
RERANK_MDL = ""
ASR_MDL = ""
IMAGE2TEXT_MDL = ""
API_KEY = None
PARSERS = None
HOST_IP = None
HOST_PORT = None
SECRET_KEY = None

DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
DATABASE = decrypt_database_config(name=DATABASE_TYPE)

# authentication
AUTHENTICATION_CONF = None

# client
CLIENT_AUTHENTICATION = None
HTTP_APP_KEY = None
GITHUB_OAUTH = None
FEISHU_OAUTH = None

DOC_ENGINE = None
docStoreConn = None

retrievaler = None
kg_retrievaler = None


def init_settings():
    global LLM, LLM_FACTORY, LLM_BASE_URL, LIGHTEN, DATABASE_TYPE, DATABASE
    LIGHTEN = int(os.environ.get('LIGHTEN', "0"))
    DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
    DATABASE = decrypt_database_config(name=DATABASE_TYPE)
    LLM = get_base_config("user_default_llm", {})
    LLM_FACTORY = LLM.get("factory", "ZHIPU-AI")
    LLM_BASE_URL = LLM.get("base_url")

    global CHAT_MDL, EMBEDDING_MDL, RERANK_MDL, ASR_MDL, IMAGE2TEXT_MDL
    if not LIGHTEN:
        default_llm = {
            "Tongyi-Qianwen": {
                "chat_model": "qwen-plus",
                "embedding_model": "text-embedding-v2",
                "image2text_model": "qwen-vl-max",
                "asr_model": "paraformer-realtime-8k-v1",
            },
            "OpenAI": {
                "chat_model": "gpt-3.5-turbo",
                "embedding_model": "text-embedding-ada-002",
                "image2text_model": "gpt-4-vision-preview",
                "asr_model": "whisper-1",
            },
            "Azure-OpenAI": {
                "chat_model": "gpt-35-turbo",
                "embedding_model": "text-embedding-ada-002",
                "image2text_model": "gpt-4-vision-preview",
                "asr_model": "whisper-1",
            },
            "ZHIPU-AI": {
                "chat_model": "glm-4-plus",
                "embedding_model": "embedding-2",
                "image2text_model": "glm-4v-plus",
                "asr_model": "",
            },
            "Ollama": {
                "chat_model": "qwen-14B-chat",
                "embedding_model": "flag-embedding",
                "image2text_model": "",
                "asr_model": "",
            },
            "Moonshot": {
                "chat_model": "moonshot-v1-8k",
                "embedding_model": "",
                "image2text_model": "",
                "asr_model": "",
            },
            "DeepSeek": {
                "chat_model": "deepseek-chat",
                "embedding_model": "",
                "image2text_model": "",
                "asr_model": "",
            },
            "VolcEngine": {
                "chat_model": "",
                "embedding_model": "",
                "image2text_model": "",
                "asr_model": "",
            },
            "BAAI": {
                "chat_model": "",
                "embedding_model": "BAAI/bge-large-zh-v1.5",
                "image2text_model": "",
                "asr_model": "",
                "rerank_model": "BAAI/bge-reranker-v2-m3",
            },
            "Youdao": {
                "chat_model": "",
                "embedding_model": "maidalun/bce-embedding-base_v1",
                "image2text_model": "",
                "asr_model": "",
                "rerank_model": "maidalun/bce-reranker-base_v1",
            }
        }

        if LLM_FACTORY:
            CHAT_MDL = default_llm[LLM_FACTORY]["chat_model"] + f"@{LLM_FACTORY}"
            ASR_MDL = default_llm[LLM_FACTORY]["asr_model"] + f"@{LLM_FACTORY}"
            IMAGE2TEXT_MDL = default_llm[LLM_FACTORY]["image2text_model"] + f"@{LLM_FACTORY}"
        EMBEDDING_MDL = default_llm["Youdao"]["embedding_model"] + "@Youdao"
        RERANK_MDL = default_llm["Youdao"]["rerank_model"] + "@Youdao"

    global API_KEY, PARSERS, HOST_IP, HOST_PORT, SECRET_KEY
    API_KEY = LLM.get("api_key", "")
    PARSERS = LLM.get(
        "parsers",
        "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email")

    HOST_IP = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("host", "127.0.0.1")
    HOST_PORT = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("http_port")

    SECRET_KEY = get_base_config(
        MULTI_RAG_SERVICE_NAME,
        {}).get("secret_key", str(date.today()))
    # SECRET_KEY = get_base_config(
    #     MULTI_RAG_SERVICE_NAME,
    #     {}).get(
    #         "secret_key",
    #     "multirag_secret_key")

    global AUTHENTICATION_CONF, CLIENT_AUTHENTICATION, HTTP_APP_KEY, GITHUB_OAUTH, FEISHU_OAUTH
    # authentication
    AUTHENTICATION_CONF = get_base_config("authentication", {})

    # client
    CLIENT_AUTHENTICATION = AUTHENTICATION_CONF.get(
        "client", {}).get(
        "switch", False)
    HTTP_APP_KEY = AUTHENTICATION_CONF.get("client", {}).get("http_app_key")
    GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
    FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")

    global DOC_ENGINE, docStoreConn, retrievaler, kg_retrievaler
    DOC_ENGINE = os.environ.get('DOC_ENGINE', "milvus")
    if DOC_ENGINE == "milvus":
        docStoreConn = core.utils.milvus_conn.MILVUS_CONNECTION
    elif DOC_ENGINE == "infinity":
        docStoreConn = core.utils.infinity_conn.InfinityConnection()
    else:
        raise Exception(f"Not supported doc engine: {DOC_ENGINE}")

    retrievaler = search.Dealer(docStoreConn)
    kg_retrievaler = kg_search.KGSearch(docStoreConn)

    # retrievaler = search.Dealer(MILVUS_CONNECTION)
    # kg_retrievaler = kg_search.KGSearch(MILVUS_CONNECTION)


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

# AIFORBI
AIFORBI_BASE_CONFIG = get_base_config("aiforbi", {})
AIFORBI_BASE_URL = AIFORBI_BASE_CONFIG.get("base_url")
AIFORBI_API_KEY = AIFORBI_BASE_CONFIG.get("api_key")
AIFORBI_MODEL_ID = AIFORBI_BASE_CONFIG.get("model_id")

AI_TRANSLATE_BASE_CONFIG = get_base_config("ai_translate", {})
AI_TRANSLATE_BASE_URL = AI_TRANSLATE_BASE_CONFIG.get("base_url")
AI_TRANSLATE_API_KEY = AI_TRANSLATE_BASE_CONFIG.get("api_key")
AI_TRANSLATE_MODEL_ID = AI_TRANSLATE_BASE_CONFIG.get("model_id")





# LLM = get_base_config("user_default_llm", {})
# LLM_FACTORY = LLM.get("factory", "ZHIPU-AI")
# LLM_BASE_URL = LLM.get("base_url")
#
# if not LIGHTEN:
#     default_llm = {
#         "Tongyi-Qianwen": {
#             "chat_model": "qwen-plus",
#             "embedding_model": "text-embedding-v2",
#             "image2text_model": "qwen-vl-max",
#             "asr_model": "paraformer-realtime-8k-v1",
#         },
#         "OpenAI": {
#             "chat_model": "gpt-3.5-turbo",
#             "embedding_model": "text-embedding-ada-002",
#             "image2text_model": "gpt-4-vision-preview",
#             "asr_model": "whisper-1",
#         },
#         "Azure-OpenAI": {
#             "chat_model": "gpt-35-turbo",
#             "embedding_model": "text-embedding-ada-002",
#             "image2text_model": "gpt-4-vision-preview",
#             "asr_model": "whisper-1",
#         },
#         "ZHIPU-AI": {
#             "chat_model": "glm-4-plus",
#             "embedding_model": "embedding-2",
#             "image2text_model": "glm-4v-plus",
#             "asr_model": "",
#         },
#         "Ollama": {
#             "chat_model": "qwen-14B-chat",
#             "embedding_model": "flag-embedding",
#             "image2text_model": "",
#             "asr_model": "",
#         },
#         "Moonshot": {
#             "chat_model": "moonshot-v1-8k",
#             "embedding_model": "",
#             "image2text_model": "",
#             "asr_model": "",
#         },
#         "DeepSeek": {
#             "chat_model": "deepseek-chat",
#             "embedding_model": "",
#             "image2text_model": "",
#             "asr_model": "",
#         },
#         "VolcEngine": {
#             "chat_model": "",
#             "embedding_model": "",
#             "image2text_model": "",
#             "asr_model": "",
#         },
#         "BAAI": {
#             "chat_model": "",
#             "embedding_model": "BAAI/bge-large-zh-v1.5",
#             "image2text_model": "",
#             "asr_model": "",
#             "rerank_model": "BAAI/bge-reranker-v2-m3",
#         }
#     }
#
#     CHAT_MDL = default_llm[LLM_FACTORY]["chat_model"]
#     EMBEDDING_MDL = default_llm["BAAI"]["embedding_model"]
#     RERANK_MDL = default_llm["BAAI"]["rerank_model"]
#     ASR_MDL = default_llm[LLM_FACTORY]["asr_model"]
#     IMAGE2TEXT_MDL = default_llm[LLM_FACTORY]["image2text_model"]
# else:
#     CHAT_MDL = EMBEDDING_MDL = RERANK_MDL = ASR_MDL = IMAGE2TEXT_MDL = ""
#
# API_KEY = LLM.get("api_key", "")
# PARSERS = LLM.get(
#     "parsers",
#     "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email")
#
# HOST = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("host", "127.0.0.1")
# HTTP_PORT = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("http_port")
#
# SECRET_KEY = get_base_config(
#     MULTI_RAG_SERVICE_NAME,
#     {}).get("secret_key", str(date.today()))
# # SECRET_KEY = get_base_config(
# #     MULTI_RAG_SERVICE_NAME,
# #     {}).get(
# #         "secret_key",
# #     "multirag_secret_key")
#
# DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
# DATABASE = decrypt_database_config(name="postgresql")
#
# # authentication
# AUTHENTICATION_CONF = get_base_config("authentication", {})
#
# # client
# CLIENT_AUTHENTICATION = AUTHENTICATION_CONF.get(
#     "client", {}).get(
#     "switch", False)
# HTTP_APP_KEY = AUTHENTICATION_CONF.get("client", {}).get("http_app_key")
# GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
# FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")
#
# retrievaler = search.Dealer(MILVUS_CONNECTION)
# kg_retrievaler = kg_search.KGSearch(MILVUS_CONNECTION)
#
# # AIFORBI
# AIFORBI_BASE_CONFIG = get_base_config("aiforbi", {})
# AIFORBI_BASE_URL = AIFORBI_BASE_CONFIG.get("base_url")
# AIFORBI_API_KEY = AIFORBI_BASE_CONFIG.get("api_key")
# AIFORBI_MODEL_ID = AIFORBI_BASE_CONFIG.get("model_id")
#
# AI_TRANSLATE_BASE_CONFIG = get_base_config("ai_translate", {})
# AI_TRANSLATE_BASE_URL = AI_TRANSLATE_BASE_CONFIG.get("base_url")
# AI_TRANSLATE_API_KEY = AI_TRANSLATE_BASE_CONFIG.get("api_key")
# AI_TRANSLATE_MODEL_ID = AI_TRANSLATE_BASE_CONFIG.get("model_id")
#
#
# class CustomEnum(Enum):
#     @classmethod
#     def valid(cls, value):
#         try:
#             cls(value)
#             return True
#         except BaseException:
#             return False
#
#     @classmethod
#     def values(cls):
#         return [member.value for member in cls.__members__.values()]
#
#     @classmethod
#     def names(cls):
#         return [member.name for member in cls.__members__.values()]
#
#
# class PythonDependenceName(CustomEnum):
#     Rag_Source_Code = "python"
#     Python_Env = "miniconda"
#
#
# class ModelStorage(CustomEnum):
#     REDIS = "redis"
#     MYSQL = "mysql"
#
#
# class RetCode(IntEnum, CustomEnum):
#     SUCCESS = 0
#     NOT_EFFECTIVE = 10
#     EXCEPTION_ERROR = 100
#     ARGUMENT_ERROR = 101
#     DATA_ERROR = 102
#     OPERATING_ERROR = 103
#     CONNECTION_ERROR = 105
#     RUNNING = 106
#     PERMISSION_ERROR = 108
#     AUTHENTICATION_ERROR = 109
#     UNAUTHORIZED = 401
#     SERVER_ERROR = 500
#     FORBIDDEN = 403
#     NOT_FOUND = 404


# from api.utils.log_utils import LoggerFactory, getLogger

# # Logger
# LoggerFactory.set_directory(
#     os.path.join(
#         get_project_base_directory(),
#         "logs",
#         "api"))
# # {CRITICAL: 50, FATAL:50, ERROR:40, WARNING:30, WARN:30, INFO:20, DEBUG:10, NOTSET:0}
# LoggerFactory.LEVEL = 30
#
# stat_logger = getLogger("stat")
# access_logger = getLogger("access")
# database_logger = getLogger("database")
# chat_logger = getLogger("chat")
