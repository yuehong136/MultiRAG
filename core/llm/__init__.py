from core.llm.chat_model.models.doubao_chat import DoubaoChat
from core.llm.chat_model.models.gptturbo import GptTurbo
from core.llm.chat_model.models.ollama_chat import OllamaChat
from core.llm.chat_model.models.qwen_chat import QWenChat
from core.llm.chat_model.models.zhipu_chat import ZhipuChat
from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.models.zhipu_4v import Zhipu4V
from core.llm.embedding_model.youdao_embedding import YoudaoEmbed
from core.llm.embedding_model.zhipu_embed import ZhipuEmbed
from core.llm.sequence2txt_model.azureseq2txt import AzureSeq2txt
from core.llm.sequence2txt_model.gptseq2txt import GPTSeq2txt
from core.llm.sequence2txt_model.ollamaseq2txt import OllamaSeq2txt
from core.llm.sequence2txt_model.qwenseq2txt import QWenSeq2txt
from core.llm.sequence2txt_model.xinferenceseq2txt import XinferenceSeq2txt

# from core.llm.ocr_model.models.local_cv import LocalCV
# from core.llm.ocr_model.models.zhipu_4v import Zhipu4V

EmbeddingModel = {
    # "Ollama": OllamaEmbed,
    # "OpenAI": OpenAIEmbed,
    # "Xinference": XinferenceEmbed,
    # "Tongyi-Qianwen": QWenEmbed,
    "ZHIPU-AI": ZhipuEmbed,
    # "FastEmbed": FastEmbed,
    "Youdao": YoudaoEmbed,
    # "BaiChuan": BaiChuanEmbed,
    # "Jina": JinaEmbed,
    # "BAAI": DefaultEmbedding,
    # "Mistral": MistralEmbed
}

CvModel = {
    "OpenAI": GptV4,
    # "Azure-OpenAI": AzureGptV4,
    # "Ollama": OllamaCV,
    # "Xinference": XinferenceCV,
    # "Tongyi-Qianwen": QWenCV,
    "ZHIPU-AI": Zhipu4V,
    # "Moonshot": LocalCV,
    # 'Gemini':GeminiCV,
    # 'OpenRouter':OpenRouterCV,
    # "LocalAI":LocalAICV
}


ChatModel = {
    "OpenAI": GptTurbo,
    "ZHIPU-AI": ZhipuChat,
    "Tongyi-Qianwen": QWenChat,
    "Ollama": OllamaChat,
    # "Xinference": XinferenceChat,
    # "Moonshot": MoonshotChat,
    # "DeepSeek": DeepSeekChat,
    "VolcEngine": DoubaoChat,
    # "BaiChuan": BaiChuanChat,
    # "MiniMax": MiniMaxChat,
    # "Mistral": MistralChat
}
#
# RerankModel = {
#     "BAAI": DefaultRerank,
#     "Jina": JinaRerank,
#     "Youdao": YoudaoRerank,
# }


Seq2txtModel = {
    "OpenAI": GPTSeq2txt,
    "Tongyi-Qianwen": QWenSeq2txt,
    "Ollama": OllamaSeq2txt,
    "Azure-OpenAI": AzureSeq2txt,
    "Xinference": XinferenceSeq2txt
}
