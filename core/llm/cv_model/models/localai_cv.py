from urllib.parse import urljoin

from openai import OpenAI

from core.llm.cv_model.base import Base
from core.llm.cv_model.models.gptv4 import GptV4


class LocalAICV(GptV4):
    _FACTORY_NAME = "LocalAI"

    def __init__(self, key, model_name, base_url, lang="Chinese", **kwargs):
        if not base_url:
            raise ValueError("Local cv model url cannot be None")
        base_url = urljoin(base_url, "v1")
        self.client = OpenAI(api_key="empty", base_url=base_url)
        self.model_name = model_name.split("___")[0]
        self.lang = lang
        Base.__init__(self, **kwargs)
