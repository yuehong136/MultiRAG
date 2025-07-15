import dashscope
import numpy as np

from api.utils.log_utils import log_exception
from core.llm.embedding_model.base import Base
from core.utils import truncate


class QWenEmbed(Base):
    def __init__(self, key, model_name="text_embedding_v4", **kwargs):
        self.key = key
        self.model_name = model_name

    def encode(self, texts: list):
        import dashscope
        batch_size = 4
        res = []
        token_count = 0
        texts = [truncate(t, 2048) for t in texts]
        for i in range(0, len(texts), batch_size):
            resp = dashscope.TextEmbedding.call(
                model=self.model_name,
                input=texts[i:i + batch_size],
                api_key=self.key,
                text_type="document"
            )
            try:
                embds = [[] for _ in range(len(resp["output"]["embeddings"]))]
                for e in resp["output"]["embeddings"]:
                    embds[e["text_index"]] = e["embedding"]
                res.extend(embds)
                token_count += self.total_token_count(resp)
            except Exception as _e:
                log_exception(_e, resp)
                raise
        return np.array(res), token_count

    def encode_queries(self, text):
        resp = dashscope.TextEmbedding.call(
            model=self.model_name,
            input=text[:2048],
            api_key=self.key,
            text_type="query"
        )
        try:
            return np.array(resp["output"]["embeddings"][0]
                            ["embedding"]), self.total_token_count(resp)
        except Exception as _e:
            log_exception(_e, resp)
