from core.llm.chat_model.models.anthropic_chat import AnthropicChat
from core.llm.chat_model.models.azure_chat import AzureChat
from core.llm.chat_model.models.baiduyiyan_chat import BaiduYiyanChat
from core.llm.chat_model.models.deepseek_chat import DeepSeekChat
from core.llm.chat_model.models.doubao_chat import DoubaoChat
from core.llm.chat_model.models.gemini_chat import GeminiChat
from core.llm.chat_model.models.gptturbo import GptTurbo
from core.llm.chat_model.models.gpustack_chat import GPUStackChat
from core.llm.chat_model.models.grop_chat import GroqChat
from core.llm.chat_model.models.huggingface_chat import HuggingFaceChat
from core.llm.chat_model.models.hunyuan_chat import HunyuanChat
from core.llm.chat_model.models.ollama_chat import OllamaChat
from core.llm.chat_model.models.openai_api_chat import OpenAI_APIChat
from core.llm.chat_model.models.openrouter_chat import OpenRouterChat
from core.llm.chat_model.models.qwen_chat import QWenChat
from core.llm.chat_model.models.siliconflow_chat import SILICONFLOWChat
from core.llm.chat_model.models.spark_chat import SparkChat
from core.llm.chat_model.models.volcengine_chat import VolcEngineChat
from core.llm.chat_model.models.xinference_chat import XinferenceChat
from core.llm.chat_model.models.zhipu_chat import ZhipuChat
from core.llm.cv_model.models.azuregptv4 import AzureGptV4
from core.llm.cv_model.models.gemini_cv import GeminiCV
from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.models.ollama_cv import OllamaCV
from core.llm.cv_model.models.openai_api_cv import OpenAI_APICV
from core.llm.cv_model.models.qwen_cv import QWenCV
from core.llm.cv_model.models.xinferencecv import XinferenceCV
from core.llm.cv_model.models.zhipu_4v import Zhipu4V
from core.llm.embedding_model.default_embedding import DefaultEmbedding
from core.llm.embedding_model.gpustack_embed import GPUStackEmbed
from core.llm.embedding_model.huggingface_embed import HuggingFaceEmbed
from core.llm.embedding_model.jina_embed import JinaEmbed
from core.llm.embedding_model.ollama_embed import OllamaEmbed
from core.llm.embedding_model.openai_api_embed import OpenAI_APIEmbed
from core.llm.embedding_model.openai_embed import OpenAIEmbed
from core.llm.embedding_model.qwen_embed import QWenEmbed
from core.llm.embedding_model.siliconflow_embed import SILICONFLOWEmbed
from core.llm.embedding_model.volcengine_embed import VolcEngineEmbed
from core.llm.embedding_model.xinference_embed import XinferenceEmbed
from core.llm.embedding_model.youdao_embedding import YoudaoEmbed
from core.llm.embedding_model.zhipu_embed import ZhipuEmbed
from core.llm.rerank_model.default_rerank import DefaultRerank
from core.llm.rerank_model.gpustack_rerank import GPUStackRerank
from core.llm.rerank_model.jina_rerank import JinaRerank
from core.llm.rerank_model.localai_rerank import LocalAIRerank
from core.llm.rerank_model.openai_api_rerank import OpenAI_APIRerank
from core.llm.rerank_model.qwen_rerank import QWenRerank
from core.llm.rerank_model.xinference_rerank import XInferenceRerank
from core.llm.rerank_model.youdao_rerank import YoudaoRerank
from core.llm.sequence2txt_model.azureseq2txt import AzureSeq2txt
from core.llm.sequence2txt_model.gptseq2txt import GPTSeq2txt
from core.llm.sequence2txt_model.gpustackseq2txt import GPUStackSeq2txt
from core.llm.sequence2txt_model.ollamaseq2txt import OllamaSeq2txt
from core.llm.sequence2txt_model.qwenseq2txt import QWenSeq2txt
from core.llm.sequence2txt_model.tencentcloudseq2txt import TencentCloudSeq2txt
from core.llm.sequence2txt_model.xinferenceseq2txt import XinferenceSeq2txt
from core.llm.tts_model.models.fish_audiotts import FishAudioTTS
from core.llm.tts_model.models.gpustacktts import GPUStackTTS
from core.llm.tts_model.models.ollamatts import OllamaTTS
from core.llm.tts_model.models.openaitts import OpenAITTS
from core.llm.tts_model.models.qwentts import QwenTTS
from core.llm.tts_model.models.sparktts import SparkTTS
from core.llm.tts_model.models.xinferencetts import XinferenceTTS


EmbeddingModel = {
    "Ollama": OllamaEmbed,
    "OpenAI": OpenAIEmbed,
    "Xinference": XinferenceEmbed,
    "Tongyi-Qianwen": QWenEmbed,
    "ZHIPU-AI": ZhipuEmbed,
    "Youdao": YoudaoEmbed,
    "GPUStack": GPUStackEmbed,
    "Jina": JinaEmbed,
    "SILICONFLOW": SILICONFLOWEmbed,
    "BAAI": DefaultEmbedding,
    "OpenAI-API-Compatible": OpenAI_APIEmbed,
    "VolcEngine": VolcEngineEmbed,
    "HuggingFace": HuggingFaceEmbed
}

CvModel = {
    "OpenAI": GptV4,
    "Azure-OpenAI": AzureGptV4,
    "Ollama": OllamaCV,
    "Xinference": XinferenceCV,
    "Tongyi-Qianwen": QWenCV,
    "ZHIPU-AI": Zhipu4V,
    # "Moonshot": LocalCV,
    'Gemini':GeminiCV,
    "OpenAI-API-Compatible": OpenAI_APICV,
    # 'OpenRouter':OpenRouterCV,
    # "LocalAI":LocalAICV
}


ChatModel = {
    "OpenAI": GptTurbo,
    "Azure-OpenAI": AzureChat,
    "ZHIPU-AI": ZhipuChat,
    "Tongyi-Qianwen": QWenChat,
    "Ollama": OllamaChat,
    "Xinference": XinferenceChat,
    "SILICONFLOW": SILICONFLOWChat,
    "Anthropic": AnthropicChat,
    "Groq": GroqChat,
    "DeepSeek": DeepSeekChat,
    "VolcEngine": VolcEngineChat,
    "OpenRouter": OpenRouterChat,
    "Tencent Hunyuan": HunyuanChat,
    "BaiduYiyan": BaiduYiyanChat,
    "Gemini": GeminiChat,
    "Doubao": DoubaoChat,
    "XunFei Spark": SparkChat,
    "OpenAI-API-Compatible": OpenAI_APIChat,
    "VLLM": OpenAI_APIChat,
    "HuggingFace": HuggingFaceChat,
    "GPUStack": GPUStackChat
}

RerankModel = {
    "BAAI": DefaultRerank,
    "Jina": JinaRerank,
    "LocalAI": LocalAIRerank,
    "OpenAI-API-Compatible": OpenAI_APIRerank,
    "Youdao": YoudaoRerank,
    "Xinference": XInferenceRerank,
    "Tongyi-Qianwen": QWenRerank,
    "GPUStack": GPUStackRerank
}


Seq2txtModel = {
    "Ollama": OllamaSeq2txt,
    "OpenAI": GPTSeq2txt,
    "Tongyi-Qianwen": QWenSeq2txt,
    "Azure-OpenAI": AzureSeq2txt,
    "Xinference": XinferenceSeq2txt,
    "Tencent Cloud": TencentCloudSeq2txt,
    "GPUStack": GPUStackSeq2txt
}


TTSModel = {
    "Fish Audio": FishAudioTTS,
    "Tongyi-Qianwen": QwenTTS,
    "OpenAI":OpenAITTS,
    "XunFei Spark": SparkTTS,
    "Xinference": XinferenceTTS,
    "Ollama": OllamaTTS,
    "GPUStack": GPUStackTTS
}