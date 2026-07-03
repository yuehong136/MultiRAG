from core.llm.chat_model.base import Base


class AnthropicChat(Base):
    def __init__(self, key, model_name, base_url="https://api.anthropic.com/v1/", **kwargs):
        if not base_url:
            base_url = "https://api.anthropic.com/v1/"
        super().__init__(key, model_name, base_url=base_url, **kwargs)
