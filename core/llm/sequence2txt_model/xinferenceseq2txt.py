# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xinferenceseq2txt.py
@date：2024/8/2 9:00
@desc:
"""
import os

from openai import OpenAI

from core.llm.sequence2txt_model.base import Base


class XinferenceSeq2txt(Base):
    def __init__(self, key, model_name="", base_url=""):
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        self.client = OpenAI(api_key="xxx", base_url=base_url)
        self.model_name = model_name