from core.llm.chat_model.base import Base


class YiChat(Base):
    def __init__(self, key, model_name, base_url="https://api.lingyiwanwu.com/v1", **kwargs):
        if not base_url:
            base_url = "https://api.lingyiwanwu.com/v1"
        super().__init__(key, model_name, base_url, **kwargs)