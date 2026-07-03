import numpy as np
import requests

from common.log_utils import log_exception
from common.token_utils import truncate
from core.llm.rerank_model.base import Base


class JinaRerank(Base):
    def __init__(self, key, model_name="jina-reranker-v2-base-multilingual", base_url="https://api.jina.ai/v1/rerank"):
        self.base_url = "https://api.jina.ai/v1/rerank"
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        self.model_name = model_name

    def similarity(self, query: str, texts: list):
        texts = [truncate(t, 8196) for t in texts]
        data = {"model": self.model_name, "query": query, "documents": texts, "top_n": len(texts)}
        res = requests.post(self.base_url, headers=self.headers, json=data).json()
        rank = np.zeros(len(texts), dtype=float)
        try:
            for d in res["results"]:
                rank[d["index"]] = d["relevance_score"]
        except Exception as _e:
            log_exception(_e, res)
        return rank, self.total_token_count(res)
