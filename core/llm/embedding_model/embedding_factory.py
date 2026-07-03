# embedding_model/embedding_factory.py
from core.llm.embedding_model.default_embedding import DefaultEmbedding

# from llm.embedding_model.openai_embedding import OpenAIEmbed
# from llm.embedding_model.baichuan_embedding import BaiChuanEmbed
# from llm.embedding_model.qwen_embedding import QWenEmbed
# from llm.embedding_model.zhipu_embedding import ZhipuEmbed
# from llm.embedding_model.ollama_embedding import OllamaEmbed
# from llm.embedding_model.fast_embedding import FastEmbed
# from llm.embedding_model.xinference_embedding import XinferenceEmbed
from core.llm.embedding_model.youdao_embedding import YoudaoEmbed

# from llm.embedding_model.jina_embedding import JinaEmbed

class EmbeddingFactory:
    def __init__(self, key, model_name, base_url=None):
        self.key = key
        self.model_name = model_name
        self.base_url = base_url

    def get_embedding_instance(self):
        if self.model_name == "default":
            return DefaultEmbedding(self.key, self.model_name)
        # elif self.model_name == "openai":
        #     return OpenAIEmbed(self.key, self.model_name, self.base_url)
        # elif self.model_name == "baichuan":
        #     return BaiChuanEmbed(self.key, self.model_name, self.base_url)
        # elif self.model_name == "qwen":
        #     return QWenEmbed(self.key, self.model_name)
        # elif self.model_name == "zhipu":
        #     return ZhipuEmbed(self.key, self.model_name)
        # elif self.model_name == "ollama":
        #     return OllamaEmbed(self.key, self.model_name, base_url=self.base_url)
        # elif self.model_name == "fast":
        #     return FastEmbed(self.key, self.model_name)
        # elif self.model_name == "xinference":
        #     return XinferenceEmbed(self.key, self.model_name, self.base_url)
        elif self.model_name == "youdao":
            return YoudaoEmbed(self.key, self.model_name)
        # elif self.model_name == "jina":
        #     return JinaEmbed(self.key, self.model_name, self.base_url)
        else:
            raise ValueError("Unsupported model name")
