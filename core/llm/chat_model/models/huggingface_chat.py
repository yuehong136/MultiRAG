from urllib.parse import urljoin

from core.llm.chat_model.base import Base


class HuggingFaceChat(Base):
    def __init__(self, key=None, model_name="", base_url=""):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        base_url = urljoin(base_url, "v1")
        super().__init__(key, model_name.split("___")[0], base_url)