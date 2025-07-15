import numpy as np
from ollama import Client

from api.utils.log_utils import log_exception
from core.llm.embedding_model.base import Base


class OllamaEmbed(Base):
    def __init__(self, key, model_name, **kwargs):
        self.client = Client(host=kwargs["base_url"]) if not key or key == "x" else \
            Client(host=kwargs["base_url"], headers={"Authorization": f"Bear {key}"})
        self.model_name = model_name

    def encode(self, texts: list):
        arr = []
        tks_num = 0
        for txt in texts:
            res = self.client.embeddings(prompt=txt,
                                         model=self.model_name,
                                         options={"use_mmap": True})
            try:
                arr.append(res["embedding"])
            except Exception as _e:
                log_exception(_e, res)
            tks_num += 128
        return np.array(arr), tks_num

    def encode_queries(self, text):
        res = self.client.embeddings(prompt=text,
                                     model=self.model_name,
                                     options={"use_mmap": True})
        try:
            return np.array(res["embedding"]), 128
        except Exception as _e:
            log_exception(_e, res)