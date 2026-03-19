import numpy as np
import requests
from urllib.parse import urljoin

from common.log_utils import log_exception
from core.llm.embedding_model.base import Base
from common.token_utils import truncate


class SILICONFLOWEmbed(Base):
    _FACTORY_NAME = "SILICONFLOW"

    def __init__(self, key, model_name, base_url="https://api.siliconflow.cn/v1/embeddings"):
        normalized_base_url = (base_url or "").strip()
        if not normalized_base_url:
            normalized_base_url = "https://api.siliconflow.cn/v1/embeddings"
        if "/embeddings" not in normalized_base_url:
            normalized_base_url = urljoin(f"{normalized_base_url.rstrip('/')}/", "embeddings").rstrip("/")
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
        }
        self.base_url = normalized_base_url
        self.model_name = model_name

    def encode(self, texts: list):
        batch_size = 16
        ress = []
        token_count = 0
        for i in range(0, len(texts), batch_size):
            texts_batch = texts[i : i + batch_size]
            if self.model_name in ["BAAI/bge-large-zh-v1.5", "BAAI/bge-large-en-v1.5"]:
                # limit 512, 340 is almost safe
                texts_batch = [" " if not text.strip() else truncate(text, 340) for text in texts_batch]
            else:
                texts_batch = [" " if not text.strip() else text for text in texts_batch]

            payload = {
                "model": self.model_name,
                "input": texts_batch,
                "encoding_format": "float",
            }
            response = requests.post(self.base_url, json=payload, headers=self.headers)
            try:
                res = response.json()
                ress.extend([d["embedding"] for d in res["data"]])
                token_count += self.total_token_count(res)
            except Exception as _e:
                log_exception(_e, response)

        return np.array(ress), token_count

    def encode_queries(self, text):
        payload = {
            "model": self.model_name,
            "input": text,
            "encoding_format": "float",
        }
        response = requests.post(self.base_url, json=payload, headers=self.headers)
        try:
            res = response.json()
            return np.array(res["data"][0]["embedding"]), self.total_token_count(res)
        except Exception as _e:
            log_exception(_e, response)