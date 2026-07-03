from .basic_config import *
from .kb_config import *
from .model_config import *
from .server_config import *

OPEN_CROSS_DOMAIN = True

VERSION = "v0.0.1"

EMBEDDING_MODEL = "BCE-embedding-base_v1"


MODEL_PLATFORMS = [
    {
        "platform_name": "oneapi",
        "platform_type": "oneapi",
        "api_base_url": "http://127.0.0.1:3000/v1",
        "api_key": "sk-",
        "api_concurrencies": 5,
        "llm_models": [
            # 智谱 API
            "chatglm_pro",
            "chatglm_turbo",
            "chatglm_std",
            "chatglm_lite",
            # 千问 API
            "qwen-turbo",
            "qwen-plus",
            "qwen-max",
            "qwen-max-longcontext",
            # 千帆 API
            "ERNIE-Bot",
            "ERNIE-Bot-turbo",
            "ERNIE-Bot-4",
            # 星火 API
            "SparkDesk",
        ],
        "embed_models": [
            # 千问 API
            "text-embedding-v1",
            # 千帆 API
            "Embedding-V1",
        ],
        "image_models": [],
        "reranking_models": [],
        "speech2text_models": [],
        "tts_models": [],
    },
    {
        "platform_name": "xinference",
        "platform_type": "xinference",
        "api_base_url": "http://127.0.0.1:9997/v1",
        "api_key": "EMPT",
        "api_concurrencies": 5,
        "llm_models": [
            "chatglm3",
            "glm4-chat",
            "qwen1.5-chat",
            "qwen2-instruct",
        ],
        "embed_models": [
            "bge-large-zh-v1.5",
        ],
        "image_models": [],
        "reranking_models": [],
        "speech2text_models": [],
        "tts_models": [],
    },
]
