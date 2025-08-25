from openai import OpenAI

from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.base import Base

class StepFunCV(GptV4):
    _FACTORY_NAME = "StepFun"

    def __init__(self, key, model_name="step-1v-8k", lang="Chinese", base_url="https://api.stepfun.com/v1", **kwargs):
        if not base_url:
            base_url = "https://api.stepfun.com/v1"
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)