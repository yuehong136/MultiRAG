# rerank_model/default_rerank.py
import os
import re
import torch
import numpy as np
from FlagEmbedding import FlagReranker
from huggingface_hub import snapshot_download
from llm.rerank_model.base import Base
from api.utils.file_utils import get_home_cache_dir
from rag.utils import num_tokens_from_string, truncate

def sigmoid(x):
    return 1 / (1 + np.exp(-x))

class DefaultRerank(Base):
    _model = None

    def __init__(self, key, model_name, **kwargs):
        super().__init__(key, model_name)
        if not DefaultRerank._model:
            try:
                self._model = FlagReranker(os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z]+/", "", model_name)),
                                           use_fp16=torch.cuda.is_available())
            except Exception as e:
                model_dir = snapshot_download(repo_id=model_name,
                                              local_dir=os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z]+/", "", model_name)),
                                              local_dir_use_symlinks=False)
                self._model = FlagReranker(os.path.join(get_home_cache_dir(), model_name),
                                           use_fp16=torch.cuda.is_available())

    def similarity(self, query: str, texts: list):
        pairs = [(query, truncate(t, 2048)) for t in texts]
        token_count = 0
        for _, t in pairs:
            token_count += num_tokens_from_string(t)
        batch_size = 32
        res = []
        for i in range(0, len(pairs), batch_size):
            scores = self._model.compute_score(pairs[i:i + batch_size], max_length=2048)
            scores = sigmoid(np.array(scores)).tolist()
            res.extend(scores)
        return np.array(res), token_count
