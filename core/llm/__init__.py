import importlib
import inspect

from strenum import StrEnum


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
    MiniMax = "MiniMax"


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
    SupportedLiteLLMProvider.MiniMax: "https://api.minimaxi.com/v1",
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
    SupportedLiteLLMProvider.Anthropic: "",  # don't need a prefix
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
    SupportedLiteLLMProvider.MiniMax: "openai/",
}

ChatModel = globals().get("ChatModel", {})
CvModel = globals().get("CvModel", {})
EmbeddingModel = globals().get("EmbeddingModel", {})
RerankModel = globals().get("RerankModel", {})
Seq2txtModel = globals().get("Seq2txtModel", {})
TTSModel = globals().get("TTSModel", {})


MODULE_MAPPING = {
    "chat": ChatModel,
    "cv": CvModel,
    "embedding": EmbeddingModel,
    "rerank": RerankModel,
    "sequence2txt": Seq2txtModel,
    "tts": TTSModel,
}

package_name = __name__

for module_name, mapping_dict in MODULE_MAPPING.items():
    full_module_name = f"{package_name}.{module_name}"
    module = importlib.import_module(full_module_name)

    base_class = None
    lite_llm_base_class = None
    for name, obj in inspect.getmembers(module):
        if inspect.isclass(obj):
            if name == "Base":
                base_class = obj
            elif name == "LiteLLMBase":
                lite_llm_base_class = obj
                assert hasattr(obj, "_FACTORY_NAME"), "LiteLLMbase should have _FACTORY_NAME field."
                if hasattr(obj, "_FACTORY_NAME"):
                    if isinstance(obj._FACTORY_NAME, list):
                        for factory_name in obj._FACTORY_NAME:
                            mapping_dict[factory_name] = obj
                    else:
                        mapping_dict[obj._FACTORY_NAME] = obj

    if base_class is not None:
        for _, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, base_class) and obj is not base_class and hasattr(obj, "_FACTORY_NAME"):
                if isinstance(obj._FACTORY_NAME, list):
                    for factory_name in obj._FACTORY_NAME:
                        mapping_dict[factory_name] = obj
                else:
                    mapping_dict[obj._FACTORY_NAME] = obj


__all__ = [
    "ChatModel",
    "CvModel",
    "EmbeddingModel",
    "RerankModel",
    "Seq2txtModel",
    "TTSModel",
]
