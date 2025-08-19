from core.llm.cv_model.base import Base


class LocalCV(Base):
    _FACTORY_NAME = "Moonshot"

    def __init__(self, key, model_name="glm-4v", lang="Chinese", **kwargs):
        pass

    def describe(self, image):
        return "", 0