import logging
import os
import numpy as np
from core.llm.embedding_model.base import Base
from api.utils.file_utils import get_home_cache_dir
from core.utils import num_tokens_from_string


class YoudaoEmbed(Base):
    _client = None

    def __init__(self, key: str | None = None, model_name: str = "maidalun1020/bce-embedding-base_v1", base_url: str | None = None, **kwargs):
        # super().__init__(key, model_name)
        model_path = self.get_model_path(model_name.split("/")[-1] if "/" in model_name else model_name)
        from api import settings
        if not settings.LIGHTEN and not YoudaoEmbed._client:
            from BCEmbedding import EmbeddingModel as qanthing
            try:
                YoudaoEmbed._client = qanthing(model_name_or_path=model_path, **kwargs)
            except Exception as e:
                logging.error(f"Failed to load BCE from {model_path}: {e}")
                default_path = os.path.join(get_home_cache_dir(), model_name)
                YoudaoEmbed._client = qanthing(model_name_or_path=default_path, **kwargs)

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

    def encode(self, texts: list[str], **kwargs):
        """
        对给定的文本列表进行编码。

        :param texts: 待编码的文本列表。
        :param batch_size: 每个批次的大小。
        :param kwargs: 其他可选参数传递给 encode 方法。
        :return: 编码后的向量数组。
        """
        batch_size = 10
        res = []
        token_count = 0
        for t in texts:
            token_count += num_tokens_from_string(t)
        for i in range(0, len(texts), batch_size):
            # 禁用tqdm进度条以避免BrokenPipeError
            # encode_kwargs = {**kwargs, 'enable_tqdm': False}
            # embds = YoudaoEmbed._client.encode(texts[i:i + batch_size], **encode_kwargs)
            embds = YoudaoEmbed._client.encode(texts[i:i + batch_size], **{**kwargs, **kwargs})
            res.extend(embds)
        return np.array(res), token_count

    def encode_queries(self, text, **kwargs):
        """
        对单个查询进行编码。

        :param text: 待编码的文本。
        :param kwargs: 其他可选参数传递给 encode 方法。
        :return: 编码后的向量数组。
        """
        # 禁用tqdm进度条以避免BrokenPipeError
        # encode_kwargs = {**kwargs, 'enable_tqdm': False}
        # embds = YoudaoEmbed._client.encode([text], **encode_kwargs)
        embds = YoudaoEmbed._client.encode([text], **{**kwargs, **kwargs})
        return np.array(embds[0]), num_tokens_from_string(text)

if __name__ == '__main__':
    embedder = YoudaoEmbed()
    texts = [
        "这是一个测试文本。",
        "我们正在测试YoudaoEmbed的编码功能。",
        "希望这个测试能够顺利通过。"
    ]
    qv, c = embedder.encode(texts)
    print("Embeddings:", qv)
    print("Count:", c)
    query_text = "这是一个查询文本。"
    qv, c = embedder.encode_queries(query_text)
    print("Query Embedding:", qv)
    print("Count:", c)
