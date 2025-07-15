import numpy as np
from zhipuai import ZhipuAI

from api.utils.log_utils import log_exception
from core.llm.embedding_model.base import Base
from core.utils import truncate


class ZhipuEmbed(Base):
    def __init__(self, key, model_name="embedding-3", **kwargs):
        self.client = ZhipuAI(api_key=key)
        self.model_name = model_name

    def encode(self, texts: list):
        arr = []
        tks_num = 0
        MAX_LEN = -1
        if self.model_name.lower() == "embedding-2":
            MAX_LEN = 512
        if self.model_name.lower() == "embedding-3":
            MAX_LEN = 3072
        if MAX_LEN > 0:
            texts = [truncate(t, MAX_LEN) for t in texts]

        for txt in texts:
            res = self.client.embeddings.create(input=txt,
                                                model=self.model_name)
            try:
                arr.append(res.data[0].embedding)
                tks_num += self.total_token_count(res)
            except Exception as _e:
                log_exception(_e, res)
        return np.array(arr), tks_num

    def encode_queries(self, text):
        res = self.client.embeddings.create(input=text,
                                            model=self.model_name)
        try:
            return np.array(res.data[0].embedding), self.total_token_count(res)
        except Exception as _e:
            log_exception(_e, res)