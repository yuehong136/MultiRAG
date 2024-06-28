# embedding_model/youdao_embedding.py
import os
import numpy as np
from dataclasses import dataclass, field
from typing import List
from BCEmbedding import EmbeddingModel as qanthing
from core.llm.embedding_model.base import Base
from api.utils.file_utils import get_home_cache_dir

@dataclass
class YoudaoEmbed(Base):
    key: str = None
    model_name: str = "maidalun1020/bce-embedding-base_v1"
    _client: qanthing = field(init=False, default=None)

    def __post_init__(self):
        model_path = self.get_model_path(self.model_name.split("/")[-1] if "/" in self.model_name else self.model_name)
        if not YoudaoEmbed._client:
            try:
                print(f"LOADING BCE from {model_path}...")
                YoudaoEmbed._client = qanthing(model_name_or_path=model_path)
            except Exception as e:
                print(f"Failed to load BCE from {model_path}: {e}")
                default_path = os.path.join(get_home_cache_dir(), self.model_name)
                YoudaoEmbed._client = qanthing(model_name_or_path=default_path)

    def get_model_path(self, model_name: str) -> str:
        """
        获取模型的路径。
        首先检查模型是否在项目目录的models子目录中，如果不存在，则尝试从默认缓存目录加载。

        :param model_name: 模型的名称。
        :return: 模型的路径。
        """
        project_base = os.path.dirname(os.path.abspath(__file__))
        models_path = os.path.join(project_base, 'models', model_name)
        if os.path.exists(models_path):
            return models_path
        return os.path.join(get_home_cache_dir(), model_name)

    def encode(self, texts: List[str], batch_size: int = 10) -> np.ndarray:
        """
        对给定的文本列表进行编码。

        :param texts: 待编码的文本列表。
        :param batch_size: 每个批次的大小。
        :return: 编码后的向量数组。
        """
        res = []
        for i in range(0, len(texts), batch_size):
            embds = YoudaoEmbed._client.encode(texts[i:i + batch_size])
            res.extend(embds)
        return np.array(res)

    def encode_queries(self, text: str) -> np.ndarray:
        """
        对单个查询文本进行编码。

        :param text: 待编码的查询文本。
        :return: 编码后的向量。
        """
        embds = YoudaoEmbed._client.encode([text])
        return np.array(embds[0])

if __name__ == '__main__':
    embedder = YoudaoEmbed()
    texts = [
        "这是一个测试文本。",
        "我们正在测试YoudaoEmbed的编码功能。",
        "希望这个测试能够顺利通过。"
    ]
    embeddings = embedder.encode(texts)
    print("Embeddings:", embeddings)
    query_text = "这是一个查询文本。"
    query_embedding = embedder.encode_queries(query_text)
    print("Query Embedding:", query_embedding)
