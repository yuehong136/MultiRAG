import numpy as np

from api.utils.log_utils import log_exception
from core.llm.rerank_model.base import Base
from core.utils import num_tokens_from_string


class CoHereRerank(Base):
    def __init__(self, key, model_name, base_url=None):
        from cohere import Client

        self.client = Client(api_key=key, base_url=base_url)
        self.model_name = model_name.split("___")[0]

    def similarity(self, query: str, texts: list):
        token_count = num_tokens_from_string(query) + sum(
            [num_tokens_from_string(t) for t in texts]
        )
        res = self.client.rerank(
            model=self.model_name,
            query=query,
            documents=texts,
            top_n=len(texts),
            return_documents=False,
        )
        rank = np.zeros(len(texts), dtype=float)
        try:
            for d in res.results:
                rank[d.index] = d.relevance_score
        except Exception as _e:
            log_exception(_e, res)
        return rank, token_count