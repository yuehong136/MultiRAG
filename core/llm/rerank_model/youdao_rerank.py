import logging
import os
import threading

import numpy as np
from api.utils.file_utils import get_home_cache_dir
from core.llm.rerank_model.base import sigmoid
from core.llm.rerank_model.default_rerank import DefaultRerank
from common.token_utils import truncate, num_tokens_from_string


class YoudaoRerank(DefaultRerank):
    _model = None
    _model_lock = threading.Lock()

    def __init__(self, key: str = None, model_name="maidalun1020/bce-reranker-base_v1", **kwargs):
        self.model_name = model_name  # 修复：显式定义 model_name
        model_path = self.get_model_path(self.model_name.split("/")[-1] if "/" in self.model_name else self.model_name)
        from common import settings
        if not settings.LIGHTEN and not YoudaoRerank._model:
            from BCEmbedding import RerankerModel
            with YoudaoRerank._model_lock:
                if not YoudaoRerank._model:
                    try:
                        # YoudaoRerank._model = RerankerModel(model_name_or_path=os.path.join(
                        #     get_home_cache_dir(),
                        #     re.sub(r"^[a-zA-Z]+/", "", model_name)))
                        YoudaoRerank._model = RerankerModel(model_name_or_path=model_path)

                    except Exception as e:
                        # YoudaoRerank._model = RerankerModel(
                        #     model_name_or_path=model_name.replace(
                        #         "maidalun1020", "InfiniFlow"))
                        logging.info(f"Failed to load BCE from {model_path}: {e}")
                        default_path = os.path.join(get_home_cache_dir(), self.model_name)
                        YoudaoRerank._model = RerankerModel(model_name_or_path=default_path)

        self._model = YoudaoRerank._model
        self._dynamic_batch_size = 8
        self._min_batch_size = 1

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

    def similarity(self, query: str, texts: list):
        pairs = [(query, truncate(t, self._model.max_length)) for t in texts]
        token_count = 0
        for _, t in pairs:
            token_count += num_tokens_from_string(t)
        batch_size = 8
        res = res = self._process_batch(pairs, max_batch_size=batch_size)
        return np.array(res), token_count

if __name__ == '__main__':
    # 初始化 YoudaoRerank 实例
    reranker = YoudaoRerank()

    # 定义测试的查询和文本列表
    query = "这是一个查询文本。"
    texts = [
        "这是第一个要重排的文本。",
        "这是第二个要重排的文本。",
        "这是第三个要重排的文本。"
    ]

    # 调用 similarity 方法，获取相似度分数和 token 计数
    scores, token_count = reranker.similarity(query, texts)

    # 打印结果
    print("Similarity Scores:", scores)
    print("Token Count:", token_count)
