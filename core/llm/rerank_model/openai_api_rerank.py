from urllib.parse import urljoin

import numpy as np
import requests

from api.utils.log_utils import log_exception
from core.llm.rerank_model.base import Base
from common.token_utils import truncate, num_tokens_from_string


class OpenAI_APIRerank(Base):
    def __init__(self, key, model_name, base_url):
        if base_url.find("/rerank") == -1:
            self.base_url = urljoin(base_url, "/rerank")
        else:
            self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}"
        }
        self.model_name = model_name.split("___")[0]

    def similarity(self, query: str, texts: list):
        # noway to config Ragflow , use fix setting
        texts = [truncate(t, 500) for t in texts]
        data = {
            "model": self.model_name,
            "query": query,
            "documents": texts,
            "top_n": len(texts),
        }
        token_count = 0
        for t in texts:
            token_count += num_tokens_from_string(t)
        res = requests.post(self.base_url, headers=self.headers, json=data).json()
        rank = np.zeros(len(texts), dtype=float)
        try:
            for d in res["results"]:
                rank[d["index"]] = d["relevance_score"]
        except Exception as _e:
            log_exception(_e, res)

        # Normalize the rank values to the range 0 to 1
        min_rank = np.min(rank)
        max_rank = np.max(rank)

        # Avoid division by zero if all ranks are identical
        if not np.isclose(min_rank, max_rank, atol=1e-3):
            rank = (rank - min_rank) / (max_rank - min_rank)
        else:
            rank = np.zeros_like(rank)

        return rank, token_count
