from openai import OpenAI

from core.llm.cv_model.base import Base
from core.llm.cv_model.models.gptv4 import GptV4


class OpenRouterCV(GptV4):
    _FACTORY_NAME = "OpenRouter"

    def __init__(
            self,
            key,
            model_name,
            lang="Chinese",
            base_url="https://openrouter.ai/api/v1", **kwargs
    ):
        if not base_url:
            base_url = "https://openrouter.ai/api/v1"
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)
