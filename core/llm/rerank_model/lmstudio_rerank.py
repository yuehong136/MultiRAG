from core.llm.rerank_model.base import Base


class LmStudioRerank(Base):
    _FACTORY_NAME = "LM-Studio"

    def __init__(self, key, model_name, base_url, **kwargs):
        pass

    def similarity(self, query: str, texts: list):
        raise NotImplementedError("The LmStudioRerank has not been implement")