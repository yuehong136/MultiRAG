import numpy as np
import requests

from common.token_utils import num_tokens_from_string
from core.llm.rerank_model.default_rerank import DefaultRerank


class HuggingfaceRerank(DefaultRerank):
    @staticmethod
    def post(query: str, texts: list, url="127.0.0.1"):
        exc = None
        scores = [0 for _ in range(len(texts))]
        batch_size = 8
        for i in range(0, len(texts), batch_size):
            try:
                res = requests.post(f"http://{url}/rerank", headers={"Content-Type": "application/json"},
                                    json={"query": query, "texts": texts[i: i + batch_size],
                                          "raw_scores": False, "truncate": True})

                for o in res.json():
                    scores[o["index"] + i] = o["score"]
            except Exception as e:
                exc = e

        if exc:
            raise exc
        return np.array(scores)

    def __init__(self, key, model_name="BAAI/bge-reranker-v2-m3", base_url="http://127.0.0.1"):
        self.model_name = model_name.split("___")[0]
        self.base_url = base_url

    def similarity(self, query: str, texts: list) -> tuple[np.ndarray, int]:
        if not texts:
            return np.array([]), 0
        token_count = 0
        for t in texts:
            token_count += num_tokens_from_string(t)
        return HuggingfaceRerank.post(query, texts, self.base_url), token_count
