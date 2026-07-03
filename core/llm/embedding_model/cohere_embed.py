import numpy as np

from common.log_utils import log_exception
from core.llm.embedding_model.base import Base


class CoHereEmbed(Base):
    def __init__(self, key, model_name, base_url=None):
        from cohere import Client

        self.client = Client(api_key=key)
        self.model_name = model_name

    def encode(self, texts: list):
        batch_size = 16
        ress = []
        token_count = 0
        for i in range(0, len(texts), batch_size):
            res = self.client.embed(
                texts=texts[i : i + batch_size],
                model=self.model_name,
                input_type="search_document",
                embedding_types=["float"],
            )
            try:
                ress.extend(list(res.embeddings.float))
                token_count += res.meta.billed_units.input_tokens
            except Exception as _e:
                log_exception(_e, res)
        return np.array(ress), token_count

    def encode_queries(self, text):
        res = self.client.embed(
            texts=[text],
            model=self.model_name,
            input_type="search_query",
            embedding_types=["float"],
        )
        try:
            return np.array(res.embeddings.float[0]), int(res.meta.billed_units.input_tokens)
        except Exception as _e:
            log_exception(_e, res)
