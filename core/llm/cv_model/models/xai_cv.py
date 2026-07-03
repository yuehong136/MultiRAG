from core.llm.cv_model.models.gptv4 import GptV4


class xAICV(GptV4):
    _FACTORY_NAME = "xAI"

    def __init__(self, key, model_name="grok-3", lang="Chinese", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://api.x.ai/v1"
        super().__init__(key, model_name, lang=lang, base_url=base_url, **kwargs)
