# embedding_model/youdao_embedding.py
import os

import numpy as np
# import tiktoken
from BCEmbedding import EmbeddingModel as qanthing
from core.llm.embedding_model.base import Base
from api.utils.file_utils import get_home_cache_dir
# from rag.dialogue import num_tokens_from_string
# encoder = tiktoken.encoding_for_model("gpt-3.5-turbo")
# def num_tokens_from_string(string: str) -> int:
#     """Returns the number of tokens in a text string."""
#     num_tokens = len(encoder.encode(string))
#     return num_tokens
class YoudaoEmbed(Base):
    _client = None

    def __init__(self, key=None, model_name="maidalun1020/bce-embedding-base_v1", **kwargs):
        super().__init__(key, model_name)
        model_path = self.get_model_path(model_name.split("/")[-1] if "/" in model_name else model_name)
        if not YoudaoEmbed._client:
            try:
                print(f"LOADING BCE from {model_path}...")
                YoudaoEmbed._client = qanthing(model_name_or_path=model_path)
            except Exception as e:
                print(f"Failed to load BCE from {model_path}: {e}")
                default_path = os.path.join(get_home_cache_dir(), model_name)
                YoudaoEmbed._client = qanthing(model_name_or_path=default_path)

    def get_model_path(self, model_name):
        project_base = os.path.dirname(os.path.abspath(__file__))
        models_path = os.path.join(project_base, 'models', model_name)
        if os.path.exists(models_path):
            return models_path
        return os.path.join(get_home_cache_dir(), model_name)

    def encode(self, texts: list, batch_size=10):
        res = []
        # token_count = sum(num_tokens_from_string(t) for t in texts)
        for i in range(0, len(texts), batch_size):
            embds = YoudaoEmbed._client.encode(texts[i:i + batch_size])
            res.extend(embds)
        return np.array(res)#, token_count

    def encode_queries(self, text):
        embds = YoudaoEmbed._client.encode([text])
        return np.array(embds[0])#, num_tokens_from_string(text)

if __name__ == '__main__':
    # 设置模型名称
    # model_name = "bce-embedding-base_v1"

    # 初始化YoudaoEmbed实例
    embedder = YoudaoEmbed()

    # 测试文本
    texts = [
        "这是一个测试文本。",
        "我们正在测试YoudaoEmbed的编码功能。",
        "希望这个测试能够顺利通过。"
    ]

    # 对文本进行编码
    embeddings = embedder.encode(texts)

    # 打印结果
    print("Embeddings:", embeddings)

    # 测试单个查询文本
    query_text = "这是一个查询文本。"
    query_embedding = embedder.encode_queries(query_text)

    # 打印结果
    print("Query Embedding:", query_embedding)