from core.llm.chat_model.models.azure_chat import AzureChat
from core.llm.chat_model.models.doubao_chat import DoubaoChat
from core.llm.chat_model.models.gemini_chat import GeminiChat
from core.llm.chat_model.models.gptturbo import GptTurbo
from core.llm.chat_model.models.huggingface_chat import HuggingFaceChat
from core.llm.chat_model.models.ollama_chat import OllamaChat
from core.llm.chat_model.models.openai_api_chat import OpenAI_APIChat
from core.llm.chat_model.models.qwen_chat import QWenChat
from core.llm.chat_model.models.volcengine_chat import VolcEngineChat
from core.llm.chat_model.models.xinference_chat import XinferenceChat
from core.llm.chat_model.models.zhipu_chat import ZhipuChat
from core.llm.cv_model.models.azuregptv4 import AzureGptV4
from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.models.xinferencecv import XinferenceCV
from core.llm.cv_model.models.zhipu_4v import Zhipu4V
from core.llm.embedding_model.huggingface_embed import HuggingFaceEmbed
from core.llm.embedding_model.xinference_embed import XinferenceEmbed
from core.llm.embedding_model.youdao_embedding import YoudaoEmbed
from core.llm.embedding_model.zhipu_embed import ZhipuEmbed
from core.llm.rerank_model.default_rerank import DefaultRerank
from core.llm.rerank_model.xinference_rerank import XInferenceRerank
from core.llm.rerank_model.youdao_rerank import YoudaoRerank
from core.llm.sequence2txt_model.azureseq2txt import AzureSeq2txt
from core.llm.sequence2txt_model.gptseq2txt import GPTSeq2txt
from core.llm.sequence2txt_model.ollamaseq2txt import OllamaSeq2txt
from core.llm.sequence2txt_model.qwenseq2txt import QWenSeq2txt
from core.llm.sequence2txt_model.tencentcloudseq2txt import TencentCloudSeq2txt
from core.llm.sequence2txt_model.xinferenceseq2txt import XinferenceSeq2txt
from core.llm.tts_model.models.fish_audiotts import FishAudioTTS
from core.llm.tts_model.models.openaitts import OpenAITTS
from core.llm.tts_model.models.qwentts import QwenTTS
from core.llm.tts_model.models.sparktts import SparkTTS
from core.llm.tts_model.models.xinferencetts import XinferenceTTS

# from core.llm.ocr_model.models.local_cv import LocalCV
# from core.llm.ocr_model.models.zhipu_4v import Zhipu4V

EmbeddingModel = {
    # "Ollama": OllamaEmbed,
    # "OpenAI": OpenAIEmbed,
    "Xinference": XinferenceEmbed,
    # "Tongyi-Qianwen": QWenEmbed,
    "ZHIPU-AI": ZhipuEmbed,
    # "FastEmbed": FastEmbed,
    "Youdao": YoudaoEmbed,
    # "BaiChuan": BaiChuanEmbed,
    # "Jina": JinaEmbed,
    # "BAAI": DefaultEmbedding,
    # "Mistral": MistralEmbed,
    "HuggingFace": HuggingFaceEmbed
}

CvModel = {
    "OpenAI": GptV4,
    "Azure-OpenAI": AzureGptV4,
    # "Ollama": OllamaCV,
    "Xinference": XinferenceCV,
    # "Tongyi-Qianwen": QWenCV,
    "ZHIPU-AI": Zhipu4V,
    # "Moonshot": LocalCV,
    # 'Gemini':GeminiCV,
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
    # "Moonshot": MoonshotChat,
    # "DeepSeek": DeepSeekChat,
    "VolcEngine": VolcEngineChat,
    # "BaiChuan": BaiChuanChat,
    # "MiniMax": MiniMaxChat,
    # "Mistral": MistralChat,
    "Gemini": GeminiChat,
    "Doubao": DoubaoChat,
    "OpenAI-API-Compatible": OpenAI_APIChat,
    "HuggingFace": HuggingFaceChat
}

RerankModel = {
    "BAAI": DefaultRerank,
    # "Jina": JinaRerank,
    "Youdao": YoudaoRerank,
    "Xinference": XInferenceRerank,
}


Seq2txtModel = {
    "Ollama": OllamaSeq2txt,
    "OpenAI": GPTSeq2txt,
    "Tongyi-Qianwen": QWenSeq2txt,
    "Azure-OpenAI": AzureSeq2txt,
    "Xinference": XinferenceSeq2txt,
    "Tencent Cloud": TencentCloudSeq2txt
}


TTSModel = {
    "Fish Audio": FishAudioTTS,
    "Tongyi-Qianwen": QwenTTS,
    "OpenAI":OpenAITTS,
    "XunFei Spark": SparkTTS,
    "Xinference": XinferenceTTS
}