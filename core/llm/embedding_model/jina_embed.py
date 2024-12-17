import numpy as np
import requests

from core.llm.embedding_model.base import Base
from core.utils import truncate


class JinaEmbed(Base):
    def __init__(self, key, model_name="jina-embeddings-v3",
                 base_url="https://api.jina.ai/v1/embeddings"):

        self.base_url = "https://api.jina.ai/v1/embeddings"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}"
        }
        self.model_name = model_name

    def encode(self, texts: list, batch_size=None):
        texts = [truncate(t, 8196) for t in texts]
        data = {
            "model": self.model_name,
            "input": texts,
            'encoding_type': 'float'
        }
        res = requests.post(self.base_url, headers=self.headers, json=data).json()
        return np.array([d["embedding"] for d in res["data"]]), res["usage"]["total_tokens"]

    def encode_queries(self, text):
        embds, cnt = self.encode([text])
        return np.array(embds[0]), cnt