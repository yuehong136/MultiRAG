from core.llm.rerank_model.base import Base


class TogetherAIRerank(Base):
    _FACTORY_NAME = "TogetherAI"

    def __init__(self, key, model_name, base_url, **kwargs):
        pass

    def similarity(self, query: str, texts: list):
        raise NotImplementedError("The api has not been implement")