from urllib.parse import urljoin

import numpy as np
from openai import OpenAI

from common.log_utils import log_exception
from core.llm.embedding_model.base import Base


class XinferenceEmbed(Base):
    def __init__(self, key, model_name="", base_url=""):
        base_url = urljoin(base_url, "v1")
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name

    def encode(self, texts: list):
        batch_size = 16
        ress = []
        total_tokens = 0
        for i in range(0, len(texts), batch_size):
            res = None
            try:
                res = self.client.embeddings.create(input=texts[i : i + batch_size], model=self.model_name)
                ress.extend([d.embedding for d in res.data])
                total_tokens += self.total_token_count(res)
            except Exception as _e:
                log_exception(_e, res)
        return np.array(ress), total_tokens

    def encode_queries(self, text):
        res = None
        try:
            res = self.client.embeddings.create(input=[text], model=self.model_name)
            return np.array(res.data[0].embedding), self.total_token_count(res)
        except Exception as _e:
            log_exception(_e, res)