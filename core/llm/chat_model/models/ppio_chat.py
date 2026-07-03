from core.llm.chat_model.base import Base


class PPIOChat(Base):
    def __init__(self, key, model_name, base_url="https://api.ppinfra.com/v3/openai", **kwargs):
        if not base_url:
            base_url = "https://api.ppinfra.com/v3/openai"
        super().__init__(key, model_name, base_url, **kwargs)
