import os
from datetime import date
from enum import IntEnum, Enum
from api.utils.file_utils import get_project_base_directory
from api.utils.log_utils import LoggerFactory, getLogger
from core.nlp import search
from graphrag import search as kg_search
from core.utils.milvus_conn import MILVUS_CONNECTION

# Logger
LoggerFactory.set_directory(
    os.path.join(
        get_project_base_directory(),
        "logs",
        "api"))
# {CRITICAL: 50, FATAL:50, ERROR:40, WARNING:30, WARN:30, INFO:20, DEBUG:10, NOTSET:0}
LoggerFactory.LEVEL = 30

stat_logger = getLogger("stat")
access_logger = getLogger("access")
database_logger = getLogger("database")
chat_logger = getLogger("chat")


from api.utils import get_base_config, decrypt_database_config

API_VERSION = "v1"
MULTI_RAG_SERVICE_NAME = "multirag"
LIGHTEN = int(os.environ.get('LIGHTEN', "0"))

SUBPROCESS_STD_LOG_NAME = "std.log"

REQUEST_WAIT_SEC = 2
REQUEST_MAX_WAIT_SEC = 300

LLM = get_base_config("user_default_llm", {})
LLM_FACTORY = LLM.get("factory", "ZHIPU-AI")
LLM_BASE_URL = LLM.get("base_url")

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
            "image2text_model": "glm-4v",
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
        }
    }

    CHAT_MDL = default_llm[LLM_FACTORY]["chat_model"]
    EMBEDDING_MDL = default_llm["BAAI"]["embedding_model"]
    RERANK_MDL = default_llm["BAAI"]["rerank_model"]
    ASR_MDL = default_llm[LLM_FACTORY]["asr_model"]
    IMAGE2TEXT_MDL = default_llm[LLM_FACTORY]["image2text_model"]
else:
    CHAT_MDL = EMBEDDING_MDL = RERANK_MDL = ASR_MDL = IMAGE2TEXT_MDL = ""

API_KEY = LLM.get("api_key", "")
PARSERS = LLM.get(
    "parsers",
    "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email")

HOST = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("host", "127.0.0.1")
HTTP_PORT = get_base_config(MULTI_RAG_SERVICE_NAME, {}).get("http_port")

SECRET_KEY = get_base_config(
    MULTI_RAG_SERVICE_NAME,
    {}).get("secret_key", str(date.today()))
# SECRET_KEY = get_base_config(
#     MULTI_RAG_SERVICE_NAME,
#     {}).get(
#         "secret_key",
#     "multirag_secret_key")

DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
DATABASE = decrypt_database_config(name="postgresql")

# authentication
AUTHENTICATION_CONF = get_base_config("authentication", {})

# client
CLIENT_AUTHENTICATION = AUTHENTICATION_CONF.get(
    "client", {}).get(
    "switch", False)
HTTP_APP_KEY = AUTHENTICATION_CONF.get("client", {}).get("http_app_key")
GITHUB_OAUTH = get_base_config("oauth", {}).get("github")
FEISHU_OAUTH = get_base_config("oauth", {}).get("feishu")

retrievaler = search.Dealer(MILVUS_CONNECTION)
kg_retrievaler = kg_search.KGSearch(MILVUS_CONNECTION)

# AIFORBI
AIFORBI_BASE_CONFIG = get_base_config("aiforbi", {})
AIFORBI_BASE_URL = AIFORBI_BASE_CONFIG.get("base_url")
AIFORBI_API_KEY = AIFORBI_BASE_CONFIG.get("api_key")
AIFORBI_MODEL_ID = AIFORBI_BASE_CONFIG.get("model_id")

AI_TRANSLATE_BASE_CONFIG = get_base_config("ai_translate", {})
AI_TRANSLATE_BASE_URL = AI_TRANSLATE_BASE_CONFIG.get("base_url")
AI_TRANSLATE_API_KEY = AI_TRANSLATE_BASE_CONFIG.get("api_key")
AI_TRANSLATE_MODEL_ID = AI_TRANSLATE_BASE_CONFIG.get("model_id")


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


class PythonDependenceName(CustomEnum):
    Rag_Source_Code = "python"
    Python_Env = "miniconda"


class ModelStorage(CustomEnum):
    REDIS = "redis"
    MYSQL = "mysql"


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
