"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from ollama import Client

from core.llm.sequence2txt_model.base import Base


class OllamaSeq2txt(Base):
    def __init__(self, key, model_name, lang="Chinese", **kwargs):
        self.client = Client(host=kwargs["base_url"])
        self.model_name = model_name
        self.lang = lang
