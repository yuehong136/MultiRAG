import numpy as np
import requests
from yarl import URL
import httpx

from api.utils.log_utils import log_exception
from core.llm.rerank_model.base import Base
from core.utils import num_tokens_from_string


class GPUStackRerank(Base):
    def __init__(
            self, key, model_name, base_url
    ):
        if not base_url:
            raise ValueError("url cannot be None")

        self.model_name = model_name
        self.base_url = str(URL(base_url) / "v1" / "rerank")
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
        }

    def similarity(self, query: str, texts: list):
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": texts,
            "top_n": len(texts),
        }

        try:
            response = requests.post(
                self.base_url, json=payload, headers=self.headers
            )
            response.raise_for_status()
            response_json = response.json()

            rank = np.zeros(len(texts), dtype=float)

            token_count = 0
            for t in texts:
                token_count += num_tokens_from_string(t)
            try:
                for result in response_json["results"]:
                    rank[result["index"]] = result["relevance_score"]
            except Exception as _e:
                log_exception(_e, response)

            return (
                rank,
                token_count,
            )

        except httpx.HTTPStatusError as e:
            raise ValueError(
                f"Error calling GPUStackRerank model {self.model_name}: {e.response.status_code} - {e.response.text}")

