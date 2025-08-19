# coding=utf-8
"""
@project: multirag
@Author：龙
@file： core.llm.cv_model.models.zhipu_4v.py.py
@date：2024/7/22 9:38
@desc:
"""
from zhipuai import ZhipuAI

from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.base import Base


class Zhipu4V(GptV4):
    _FACTORY_NAME = "ZHIPU-AI"

    def __init__(self, key, model_name="glm-4v", lang="Chinese", **kwargs):
        self.client = ZhipuAI(api_key=key)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)