from core.llm.cv_model.models.gptv4 import GptV4


class BaiduYiyanCV(GptV4):
    def __init__(self, key, model_name, lang="Chinese", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://qianfan.baidubce.com/v2"
        super().__init__(key, model_name, lang=lang, base_url=base_url, **kwargs)
