import os
from urllib.parse import urljoin
import numpy as np
import requests

from core.llm.rerank_model.base import Base


class XInferenceRerank(Base):
    def __init__(self, key="xxxxxxx", model_name="", base_url=""):
        if base_url.find("/v1") == -1:
            base_url = urljoin(base_url, "/v1/rerank")
        self.model_name = model_name
        self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json",
            "accept": "application/json"
        }

    def similarity(self, query: str, texts: list):
        if len(texts) == 0:
            return np.array([]), 0
        data = {
            "model": self.model_name,
            "query": query,
            "return_documents": "true",
            "return_len": "true",
            "documents": texts
        }
        res = requests.post(self.base_url, headers=self.headers, json=data).json()
        rank = np.zeros(len(texts), dtype=float)
        for d in res["results"]:
            rank[d["index"]] = d["relevance_score"]
        return rank, res["meta"]["tokens"]["input_tokens"] + res["meta"]["tokens"]["output_tokens"]
