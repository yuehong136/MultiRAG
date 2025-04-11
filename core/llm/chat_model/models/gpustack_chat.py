import os

from core.llm.chat_model.base import Base


class GPUStackChat(Base):
    def __init__(self, key=None, model_name="", base_url=""):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        if base_url.split("/")[-1] != "v1-openai":
            base_url = os.path.join(base_url, "v1-openai")
        super().__init__(key, model_name, base_url)