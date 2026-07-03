from core.llm.cv_model.models.gptv4 import GptV4


class TogetherAICV(GptV4):
    _FACTORY_NAME = "TogetherAI"

    def __init__(self, key, model_name, lang="Chinese", base_url="https://api.together.xyz/v1", **kwargs):
        if not base_url:
            base_url = "https://api.together.xyz/v1"
        super().__init__(key, model_name, lang, base_url, **kwargs)
