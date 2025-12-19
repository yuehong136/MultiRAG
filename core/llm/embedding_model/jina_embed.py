import numpy as np
import requests

from common.log_utils import log_exception
from core.llm.embedding_model.base import Base
from common.token_utils import truncate


class JinaEmbed(Base):
    def __init__(self, key, model_name="jina-embeddings-v3",
                 base_url="https://api.jina.ai/v1/embeddings"):

        self.base_url = "https://api.jina.ai/v1/embeddings"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}"
        }
        self.model_name = model_name

    def encode(self, texts: list):
        texts = [truncate(t, 8196) for t in texts]
        batch_size = 16
        ress = []
        token_count = 0
        for i in range(0, len(texts), batch_size):
            data = {
                "model": self.model_name,
                "input": texts[i:i + batch_size],
                'encoding_type': 'float'
            }
            response = requests.post(self.base_url, headers=self.headers, json=data)
            try:
                res = response.json()
                ress.extend([d["embedding"] for d in res["data"]])
                token_count += self.total_token_count(res)
            except Exception as _e:
                log_exception(_e, response)
        return np.array(ress), token_count

    def encode_queries(self, text):
        embds, cnt = self.encode([text])
        return np.array(embds[0]), cnt