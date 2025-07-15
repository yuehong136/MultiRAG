from urllib.parse import urljoin

from openai import OpenAI
from core.llm.chat_model.base import Base


class LmStudioChat(Base):
    def __init__(self, key, model_name, base_url, **kwargs):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        base_url = urljoin(base_url, "v1")
        super().__init__(key, model_name, base_url, **kwargs)
        self.client = OpenAI(api_key="lm-studio", base_url=base_url)
        self.model_name = model_name