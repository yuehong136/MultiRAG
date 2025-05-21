import os

from api.db.db_models import Base
from openai import OpenAI


class LmStudioChat(Base):
    def __init__(self, key, model_name, base_url):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        super().__init__(key, model_name, base_url)
        self.client = OpenAI(api_key="lm-studio", base_url=base_url)
        self.model_name = model_name