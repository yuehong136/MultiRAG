import os

import numpy as np
import requests

from core.llm.rerank_model.base import Base


class XInferenceRerank(Base):
    def __init__(self, key="xxxxxxx", model_name="", base_url=""):
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
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
        return np.array([d["relevance_score"] for d in res["results"]]), res["meta"]["tokens"]["input_tokens"]+res["meta"]["tokens"]["output_tokens"]
