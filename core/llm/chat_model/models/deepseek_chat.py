from core.llm.chat_model.base import Base


class DeepSeekChat(Base):
    def __init__(self, key, model_name="deepseek-chat", base_url="https://api.deepseek.com/v1"):
        if not base_url:
            base_url = "https://api.deepseek.com/v1"
        super().__init__(key, model_name, base_url)