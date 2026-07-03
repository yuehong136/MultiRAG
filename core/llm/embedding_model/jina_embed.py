import base64

import numpy as np
import requests

from common.log_utils import log_exception
from common.token_utils import truncate
from core.llm.embedding_model.base import Base


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


class JinaMultiVecEmbed(Base):
    _FACTORY_NAME = "Jina"

    def __init__(self, key, model_name="jina-embeddings-v4", base_url="https://api.jina.ai/v1/embeddings"):
        self.base_url = "https://api.jina.ai/v1/embeddings"
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        self.model_name = model_name

    def encode(self, texts: list[str | bytes], task="retrieval.passage"):
        batch_size = 16
        ress = []
        token_count = 0
        input = []
        for text in texts:
            if isinstance(text, str):
                input.append({"text": text})
            elif isinstance(text, bytes):
                img_b64s = None
                try:
                    base64.b64decode(text, validate=True)
                    img_b64s = text.decode('utf8')
                except Exception:
                    img_b64s = base64.b64encode(text).decode('utf8')
                input.append({"image": img_b64s})  # base64 encoded image
        for i in range(0, len(texts), batch_size):
            data = {"model": self.model_name, "task": task, "truncate": True, "return_multivector": True, "input": input[i: i + batch_size]}
            response = requests.post(self.base_url, headers=self.headers, json=data)
            try:
                res = response.json()
                ress.extend([d["embeddings"] for d in res["data"]])
                token_count += self.total_token_count(res)
            except Exception as _e:
                log_exception(_e, response)
        return np.array(ress), token_count

    def encode_queries(self, text):
        embds, cnt = self.encode([text], task="retrieval.query")
        return np.array(embds[0]), cnt
