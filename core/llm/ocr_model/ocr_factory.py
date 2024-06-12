# from core.llm.ocr_model.models.gpt_v4 import GptV4
# from core.llm.ocr_model.models.qwen_cv import QWenCV
from core.llm.ocr_model.models.zhipu_4v import Zhipu4V
# from core.llm.ocr_model.models.ollama_cv import OllamaCV
# from core.llm.ocr_model.models.xinference_cv import XinferenceCV
from core.llm.ocr_model.models.local_cv import LocalCV

class ModelFactory:
    @staticmethod
    def get_model(model_type, key, model_name, lang="Chinese", **kwargs):
        if model_type == "zhipu_4v":
            return Zhipu4V(key, model_name, lang, **kwargs)
        # elif model_type == "qwen_cv":
        #     return QWenCV(key, model_name, lang, **kwargs)
        # elif model_type == "gpt_v4":
        #     return GptV4(key, model_name, lang, **kwargs)
        # elif model_type == "ollama_cv":
        #     return OllamaCV(key, model_name, lang, **kwargs)
        # elif model_type == "xinference_cv":
        #     return XinferenceCV(key, model_name, lang, **kwargs)
        elif model_type == "local_cv":
            return LocalCV(key, model_name, lang, **kwargs)
        else:
            raise ValueError("Unknown model type")
