import importlib
import inspect
from enum import StrEnum
from threading import RLock
from typing import Any


class SupportedLiteLLMProvider(StrEnum):
    Tongyi_Qianwen = "Tongyi-Qianwen"
    Dashscope = "Dashscope"
    Bedrock = "Bedrock"
    Moonshot = "Moonshot"
    xAI = "xAI"
    DeepInfra = "DeepInfra"
    Groq = "Groq"
    Cohere = "Cohere"
    Gemini = "Gemini"
    DeepSeek = "DeepSeek"
    Nvidia = "NVIDIA"
    TogetherAI = "TogetherAI"
    Anthropic = "Anthropic"
    Ollama = "Ollama"
    LongCat = "LongCat"
    CometAPI = "CometAPI"
    SILICONFLOW = "SILICONFLOW"
    OpenRouter = "OpenRouter"
    StepFun = "StepFun"
    PPIO = "PPIO"
    PerfXCloud = "PerfXCloud"
    Upstage = "Upstage"
    NovitaAI = "NovitaAI"
    Lingyi_AI = "01.AI"
    GiteeAI = "GiteeAI"
    AI_302 = "302.AI"
    JiekouAI = "Jiekou.AI"
    ZHIPU_AI = "ZHIPU-AI"
    MiniMax = "MiniMax"
    DeerAPI = "DeerAPI"
    GPUStack = "GPUStack"
    OpenAI = "OpenAI"
    Azure_OpenAI = "Azure-OpenAI"
    BaiduYiyan = "BaiduYiyan"
    n1n = "n1n"
    HunYuan = "Tencent Hunyuan"
    Avian = "Avian"
    Astraflow = "Astraflow"
    Astraflow_CN = "Astraflow-CN"
    FuturMix = "FuturMix"


FACTORY_DEFAULT_BASE_URL = {
    SupportedLiteLLMProvider.Tongyi_Qianwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    SupportedLiteLLMProvider.Dashscope: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    SupportedLiteLLMProvider.Moonshot: "https://api.moonshot.cn/v1",
    SupportedLiteLLMProvider.Ollama: "",
    SupportedLiteLLMProvider.LongCat: "https://api.longcat.chat/openai",
    SupportedLiteLLMProvider.CometAPI: "https://api.cometapi.com/v1",
    SupportedLiteLLMProvider.SILICONFLOW: "https://api.siliconflow.cn/v1",
    SupportedLiteLLMProvider.OpenRouter: "https://openrouter.ai/api/v1",
    SupportedLiteLLMProvider.StepFun: "https://api.stepfun.com/v1",
    SupportedLiteLLMProvider.PPIO: "https://api.ppinfra.com/v3/openai",
    SupportedLiteLLMProvider.PerfXCloud: "https://cloud.perfxlab.cn/v1",
    SupportedLiteLLMProvider.Upstage: "https://api.upstage.ai/v1/solar",
    SupportedLiteLLMProvider.NovitaAI: "https://api.novita.ai/v3/openai",
    SupportedLiteLLMProvider.Lingyi_AI: "https://api.lingyiwanwu.com/v1",
    SupportedLiteLLMProvider.GiteeAI: "https://ai.gitee.com/v1/",
    SupportedLiteLLMProvider.AI_302: "https://api.302.ai/v1",
    SupportedLiteLLMProvider.Anthropic: "https://api.anthropic.com/",
    SupportedLiteLLMProvider.JiekouAI: "https://api.jiekou.ai/openai",
    SupportedLiteLLMProvider.ZHIPU_AI: "https://open.bigmodel.cn/api/paas/v4",
    SupportedLiteLLMProvider.MiniMax: "https://api.minimaxi.com/v1",
    SupportedLiteLLMProvider.DeerAPI: "https://api.deerapi.com/v1",
    SupportedLiteLLMProvider.OpenAI: "https://api.openai.com/v1",
    SupportedLiteLLMProvider.BaiduYiyan: "https://qianfan.baidubce.com/v2",
    SupportedLiteLLMProvider.n1n: "https://api.n1n.ai/v1",
    SupportedLiteLLMProvider.HunYuan: "https://api.hunyuan.cloud.tencent.com/v1",
    SupportedLiteLLMProvider.Avian: "https://api.avian.io/v1",
    SupportedLiteLLMProvider.Astraflow: "https://api-us-ca.umodelverse.ai/v1",
    SupportedLiteLLMProvider.Astraflow_CN: "https://api.modelverse.cn/v1",
    SupportedLiteLLMProvider.FuturMix: "https://futurmix.ai/v1",
}


LITELLM_PROVIDER_PREFIX = {
    SupportedLiteLLMProvider.Tongyi_Qianwen: "dashscope/",
    SupportedLiteLLMProvider.Dashscope: "dashscope/",
    SupportedLiteLLMProvider.Bedrock: "bedrock/",
    SupportedLiteLLMProvider.Moonshot: "moonshot/",
    SupportedLiteLLMProvider.xAI: "xai/",
    SupportedLiteLLMProvider.DeepInfra: "deepinfra/",
    SupportedLiteLLMProvider.Groq: "groq/",
    SupportedLiteLLMProvider.Cohere: "",  # don't need a prefix
    SupportedLiteLLMProvider.Gemini: "gemini/",
    SupportedLiteLLMProvider.DeepSeek: "deepseek/",
    SupportedLiteLLMProvider.Nvidia: "nvidia_nim/",
    SupportedLiteLLMProvider.TogetherAI: "together_ai/",
    # 必须显式带前缀：LiteLLM 靠模型名反查 provider，缺前缀时只有内置的 claude-* 能解析，
    # 智谱 /api/anthropic、百炼 /apps/anthropic 这类 Anthropic 兼容端点用的是自家模型名
    # （glm-*、qwen-*），会在 get_llm_provider 阶段就报 "LLM Provider NOT provided"。
    SupportedLiteLLMProvider.Anthropic: "anthropic/",
    SupportedLiteLLMProvider.Ollama: "ollama_chat/",
    SupportedLiteLLMProvider.LongCat: "openai/",
    SupportedLiteLLMProvider.CometAPI: "openai/",
    SupportedLiteLLMProvider.SILICONFLOW: "openai/",
    SupportedLiteLLMProvider.OpenRouter: "openai/",
    SupportedLiteLLMProvider.StepFun: "openai/",
    SupportedLiteLLMProvider.PPIO: "openai/",
    SupportedLiteLLMProvider.PerfXCloud: "openai/",
    SupportedLiteLLMProvider.Upstage: "openai/",
    SupportedLiteLLMProvider.NovitaAI: "openai/",
    SupportedLiteLLMProvider.Lingyi_AI: "openai/",
    SupportedLiteLLMProvider.GiteeAI: "openai/",
    SupportedLiteLLMProvider.AI_302: "openai/",
    SupportedLiteLLMProvider.JiekouAI: "openai/",
    SupportedLiteLLMProvider.ZHIPU_AI: "openai/",
    SupportedLiteLLMProvider.MiniMax: "openai/",
    SupportedLiteLLMProvider.DeerAPI: "openai/",
    SupportedLiteLLMProvider.GPUStack: "openai/",
    SupportedLiteLLMProvider.OpenAI: "openai/",
    SupportedLiteLLMProvider.Azure_OpenAI: "azure/",
    SupportedLiteLLMProvider.BaiduYiyan: "openai/",
    SupportedLiteLLMProvider.n1n: "openai/",
    SupportedLiteLLMProvider.HunYuan: "openai/",
    SupportedLiteLLMProvider.Avian: "openai/",
    SupportedLiteLLMProvider.Astraflow: "openai/",
    SupportedLiteLLMProvider.Astraflow_CN: "openai/",
    SupportedLiteLLMProvider.FuturMix: "openai/",
}

# Keep provider metadata available to direct chat/cv imports without loading
# every unrelated model family during application registration.
_REGISTRY_MODULES = {
    "ChatModel": "chat",
    "CvModel": "cv",
    "EmbeddingModel": "embedding",
    "RerankModel": "rerank",
    "Seq2txtModel": "sequence2txt",
    "TTSModel": "tts",
    "OcrModel": "ocr_model",
    "ModelMeta": "model_meta",
}
_REGISTRY_LOCKS = {name: RLock() for name in _REGISTRY_MODULES}


def _build_registry(module_name: str) -> dict[str, type[Any]]:
    module = importlib.import_module(f"{__name__}.{module_name}")
    mapping: dict[str, type[Any]] = {}
    base_class = None
    for name, obj in inspect.getmembers(module):
        if not inspect.isclass(obj):
            continue
        if name == "Base":
            base_class = obj
        elif name == "LiteLLMBase":
            assert hasattr(obj, "_FACTORY_NAME"), "LiteLLMbase should have _FACTORY_NAME field."
            factories = obj._FACTORY_NAME
            for factory in factories if isinstance(factories, list) else [factories]:
                mapping[factory] = obj

    if base_class is not None:
        for _, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, base_class) and obj is not base_class and hasattr(obj, "_FACTORY_NAME"):
                factories = obj._FACTORY_NAME
                for factory in factories if isinstance(factories, list) else [factories]:
                    mapping[factory] = obj
    return mapping


def __getattr__(name: str) -> dict[str, type[Any]]:
    if name not in _REGISTRY_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    with _REGISTRY_LOCKS[name]:
        existing = globals().get(name)
        if existing is not None:
            return existing
        # Publish only a complete dictionary. An import/discovery failure stays
        # visible to the caller and the next request can retry initialization.
        mapping = _build_registry(_REGISTRY_MODULES[name])
        globals()[name] = mapping
        return mapping


def __dir__() -> list[str]:
    return sorted(set(globals()) | _REGISTRY_MODULES.keys())


__all__ = [
    "ChatModel",
    "CvModel",
    "EmbeddingModel",
    "ModelMeta",
    "OcrModel",
    "RerankModel",
    "Seq2txtModel",
    "TTSModel",
]
