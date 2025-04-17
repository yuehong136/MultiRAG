# coding=utf-8
"""
@project: multirag
@Author：龙
@file： core.llm.cv_model.models.zhipu_4v.py.py
@date：2024/7/22 9:38
@desc:
"""
from dataclasses import dataclass, field
from typing import Any

from zhipuai import ZhipuAI

from core.llm.cv_model.base import Base
from core.nlp import is_english


class Zhipu4V(Base):
    def __init__(self, key, model_name="glm-4v-plus", lang="Chinese", **kwargs):
        self.client = ZhipuAI(api_key=key)
        self.model_name = model_name
        self.lang = lang


    def describe(self, image: bytes, max_tokens: int = 1024) -> tuple[str, int]:
        b64 = self.image2base64(image)
        prompt = self.prompt(b64)
        prompt[0]["content"][1]["type"] = "text"

        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=prompt
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], image: str = "") -> tuple[str, int]:
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]
        try:
            for his in history:
                if his["role"] == "user":
                    if image:
                       his["content"] = self.chat_prompt(his["content"], image)
                    else:
                        his["content"] = self.chat_onlytext(his["content"])

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7)
            )
            return response.choices[0].message.content.strip(), response.usage.total_tokens
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], image: str = ""):
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        ans = ""
        tk_count = 0
        try:
            for his in history:
                if his["role"] == "user":
                    if image:
                       his["content"] = self.chat_prompt(his["content"], image)
                    else:
                        his["content"] = self.chat_onlytext(his["content"])
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7),
                stream=True
            )
            for resp in response:
                if not resp.choices[0].delta.content:
                    continue
                delta = resp.choices[0].delta.content
                ans += delta
                if resp.choices[0].finish_reason == "length":
                    ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                    tk_count = resp.usage.total_tokens
                if resp.choices[0].finish_reason == "stop":
                    tk_count = resp.usage.total_tokens
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count