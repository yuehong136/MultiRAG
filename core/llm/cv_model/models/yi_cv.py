from core.llm.cv_model.models.gptv4 import GptV4

class YiCV(GptV4):
    _FACTORY_NAME = "01.AI"

    def __init__(
            self,
            key,
            model_name,
            lang="Chinese",
            base_url="https://api.lingyiwanwu.com/v1", **kwargs
    ):
        if not base_url:
            base_url = "https://api.lingyiwanwu.com/v1"
        super().__init__(key, model_name, lang, base_url, **kwargs)