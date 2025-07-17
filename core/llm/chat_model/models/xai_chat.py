from core.llm.chat_model.base import Base


class xAIChat(Base):
    _FACTORY_NAME = "xAI"

    def __init__(self, key, model_name="grok-3", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://api.x.ai/v1"
        super().__init__(key, model_name, base_url=base_url, **kwargs)
        return