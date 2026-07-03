from core.llm.chat_model.base import Base


class MoonshotChat(Base):

    def __init__(self, key, model_name="moonshot-v1-8k", base_url="https://api.moonshot.cn/v1", **kwargs):
        if not base_url:
            base_url = "https://api.moonshot.cn/v1"
        super().__init__(key, model_name, base_url)
