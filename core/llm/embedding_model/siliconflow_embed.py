import numpy as np
import requests

from core.llm.embedding_model.base import Base


class SILICONFLOWEmbed(Base):
    def __init__(
        self, key, model_name, base_url="https://api.siliconflow.cn/v1/embeddings"
    ):
        if not base_url:
            base_url = "https://api.siliconflow.cn/v1/embeddings"
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
        }
        self.base_url = base_url
        self.model_name = model_name

    def encode(self, texts: list):
        batch_size = 16
        ress = []
        token_count = 0
        for i in range(0, len(texts), batch_size):
            texts_batch = texts[i : i + batch_size]
            payload = {
                "model": self.model_name,
                "input": texts_batch,
                "encoding_format": "float",
            }
            res = requests.post(self.base_url, json=payload, headers=self.headers).json()
            if "data" not in res or not isinstance(res["data"], list) or len(res["data"]) != len(texts_batch):
                raise ValueError(f"SILICONFLOWEmbed.encode got invalid response from {self.base_url}")
            ress.extend([d["embedding"] for d in res["data"]])
            token_count += self.total_token_count(res)
        return np.array(ress), token_count

    def encode_queries(self, text):
        payload = {
            "model": self.model_name,
            "input": text,
            "encoding_format": "float",
        }
        res = requests.post(self.base_url, json=payload, headers=self.headers).json()
        if "data" not in res or not isinstance(res["data"], list) or len(res["data"])!= 1:
            raise ValueError(f"SILICONFLOWEmbed.encode_queries got invalid response from {self.base_url}")
        return np.array(res["data"][0]["embedding"]), self.total_token_count(res)