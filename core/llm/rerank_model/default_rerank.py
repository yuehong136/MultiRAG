# rerank_model/default_rerank.py
import os
import re
import threading
import numpy as np
from huggingface_hub import snapshot_download
from core.llm.rerank_model.base import Base, sigmoid
from api.utils.file_utils import get_home_cache_dir
from core.utils import num_tokens_from_string, truncate


class DefaultRerank(Base):
    _model = None
    _model_lock = threading.Lock()

    def __init__(self, key, model_name, **kwargs):
        """
        If you have trouble downloading HuggingFace models, -_^ this might help!!

        For Linux:
        export HF_ENDPOINT=https://hf-mirror.com

        For Windows:
        Good luck
        ^_-

        """
        from api import settings
        if not settings.LIGHTEN and not DefaultRerank._model:
            import torch
            from FlagEmbedding import FlagReranker
            with DefaultRerank._model_lock:
                if not DefaultRerank._model:
                    try:
                        DefaultRerank._model = FlagReranker(
                            os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z0-9]+/", "", model_name)),
                            use_fp16=torch.cuda.is_available())
                    except Exception as e:
                        model_dir = snapshot_download(repo_id=model_name,
                                                      local_dir=os.path.join(get_home_cache_dir(),
                                                                             re.sub(r"^[a-zA-Z0-9]+/", "", model_name)),
                                                      local_dir_use_symlinks=False)
                        DefaultRerank._model = FlagReranker(model_dir, use_fp16=torch.cuda.is_available())
        self._model = DefaultRerank._model
        self._dynamic_batch_size = 8
        self._min_batch_size = 1

    def torch_empty_cache(self):
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception as e:
            print(f"Error emptying cache: {e}")

    def _process_batch(self, pairs, max_batch_size=None):
        """template method for subclass call"""
        old_dynamic_batch_size = self._dynamic_batch_size
        if max_batch_size is not None:
            self._dynamic_batch_size = max_batch_size
        res = []
        i = 0
        while i < len(pairs):
            current_batch = self._dynamic_batch_size
            max_retries = 5
            retry_count = 0
            while retry_count < max_retries:
                try:
                    # call subclass implemented batch processing calculation
                    batch_scores = self._compute_batch_scores(pairs[i:i + current_batch])
                    res.extend(batch_scores)
                    i += current_batch
                    self._dynamic_batch_size = min(self._dynamic_batch_size * 2, 8)
                    break
                except RuntimeError as e:
                    if "CUDA out of memory" in str(e) and current_batch > self._min_batch_size:
                        current_batch = max(current_batch // 2, self._min_batch_size)
                        self.torch_empty_cache()
                        retry_count += 1
                    else:
                        raise
            if retry_count >= max_retries:
                raise RuntimeError("max retry times, still cannot process batch, please check your GPU memory")
            self.torch_empty_cache()

        self._dynamic_batch_size = old_dynamic_batch_size
        return np.array(res)

    def _compute_batch_scores(self, batch_pairs, max_length=None):
        if max_length is None:
            max_length = self._model.max_length
        scores = self._model.compute_score(batch_pairs, max_length=max_length)
        scores = sigmoid(np.array(scores)).tolist()
        return scores

    def similarity(self, query: str, texts: list):
        pairs = [(query, truncate(t, 2048)) for t in texts]
        token_count = 0
        for _, t in pairs:
            token_count += num_tokens_from_string(t)
        batch_size = 4096
        res = self._process_batch(pairs, max_batch_size=batch_size)
        return np.array(res), token_count
