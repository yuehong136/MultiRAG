from core.llm.cv_model.models.gptv4 import GptV4


class QWenCV(GptV4):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="qwen-vl-chat-v1", lang="Chinese", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        super().__init__(key, model_name, lang=lang, base_url=base_url, **kwargs)