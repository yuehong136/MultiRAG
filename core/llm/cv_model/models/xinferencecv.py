from urllib.parse import urljoin

from openai import OpenAI

from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.base import Base


class XinferenceCV(GptV4):
    _FACTORY_NAME = "Xinference"

    def __init__(self, key, model_name="", lang="Chinese", base_url="", **kwargs):
        base_url = urljoin(base_url, "v1")
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)