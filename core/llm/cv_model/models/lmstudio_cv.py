from urllib.parse import urljoin

from openai import OpenAI

from core.llm.cv_model.base import Base
from core.llm.cv_model.models.gptv4 import GptV4


class LmStudioCV(GptV4):
    _FACTORY_NAME = "LM-Studio"

    def __init__(self, key, model_name, lang="Chinese", base_url="", **kwargs):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        base_url = urljoin(base_url, "v1")
        self.client = OpenAI(api_key="lm-studio", base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)
