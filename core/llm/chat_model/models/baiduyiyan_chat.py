from core.llm.chat_model.base import Base


class BaiduYiyanChat(Base):
    _FACTORY_NAME = "BaiduYiyan"

    def __init__(self, key, model_name, base_url=None, **kwargs):
        if not base_url:
            base_url = "https://qianfan.baidubce.com/v2"
        super().__init__(key, model_name, base_url, **kwargs)
