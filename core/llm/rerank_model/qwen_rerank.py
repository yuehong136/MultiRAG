import numpy as np

from common.log_utils import log_exception
from core.llm.rerank_model.base import Base


class QWenRerank(Base):
    def __init__(self, key, model_name='gte-rerank', base_url=None, **kwargs):
        import dashscope
        self.api_key = key
        self.model_name = dashscope.TextReRank.Models.gte_rerank if model_name is None else model_name

    def similarity(self, query: str, texts: list):
        from http import HTTPStatus

        import dashscope
        resp = dashscope.TextReRank.call(
            api_key=self.api_key,
            model=self.model_name,
            query=query,
            documents=texts,
            top_n=len(texts),
            return_documents=False
        )
        rank = np.zeros(len(texts), dtype=float)
        if resp.status_code == HTTPStatus.OK:
            try:
                for r in resp.output.results:
                    rank[r.index] = r.relevance_score
            except Exception as _e:
                log_exception(_e, resp)
            return rank, resp.usage.total_tokens
        else:
            raise ValueError(f"Error calling QWenRerank model {self.model_name}: {resp.status_code} - {resp.text}")
