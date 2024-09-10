import os

from core.llm.chat_model.base import Base


class OpenAI_APIChat(Base):
    def __init__(self, key, model_name, base_url):
        if not base_url:
            raise ValueError("url cannot be None")
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        model_name = model_name.split("___")[0]
        super().__init__(key, model_name, base_url)