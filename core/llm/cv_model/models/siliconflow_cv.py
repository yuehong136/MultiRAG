from core.llm.cv_model.models.gptv4 import GptV4


class SILICONFLOWCV(GptV4):
    _FACTORY_NAME = "SILICONFLOW"

    def __init__(self, key, model_name, lang="Chinese", base_url="https://api.siliconflow.cn/v1", **kwargs):
        if not base_url:
            base_url = "https://api.siliconflow.cn/v1"
        super().__init__(key, model_name, lang, base_url, **kwargs)
