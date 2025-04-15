from core.llm.chat_model.base import Base


class ModelScopeChat(Base):
    def __init__(self, key=None, model_name="", base_url=""):
        if not base_url:
            raise ValueError("Local llm url cannot be None")
        base_url = base_url.rstrip('/')
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        super().__init__(key, model_name.split("___")[0], base_url)