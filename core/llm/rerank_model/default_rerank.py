# rerank_model/default_rerank.py
import os
import re
import torch
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple
from FlagEmbedding import FlagReranker
from huggingface_hub import snapshot_download
from core.llm.rerank_model.base import Base
from api.utils.file_utils import get_home_cache_dir
from core.utils import num_tokens_from_string, truncate

def sigmoid(x):
    return 1 / (1 + np.exp(-x))

@dataclass
class DefaultRerank(Base):
    _model: FlagReranker = field(init=False, default=None)

    def __post_init__(self):
        model_path = os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z]+/", "", self.model_name))
        if not DefaultRerank._model:
            try:
                self._model = FlagReranker(model_path, use_fp16=torch.cuda.is_available())
            except Exception:
                model_dir = snapshot_download(
                    repo_id=self.model_name,
                    local_dir=model_path,
                    local_dir_use_symlinks=False
                )
                self._model = FlagReranker(model_dir, use_fp16=torch.cuda.is_available())

    def similarity(self, query: str, texts: List[str]) -> Tuple[np.ndarray, int]:
        pairs = [(query, truncate(t, 2048)) for t in texts]
        token_count = sum(num_tokens_from_string(t) for _, t in pairs)

        batch_size = 32
        res = []
        for i in range(0, len(pairs), batch_size):
            scores = self._model.compute_score(pairs[i:i + batch_size], max_length=2048)
            scores = sigmoid(np.array(scores)).tolist()
            res.extend(scores)
        return np.array(res), token_count
