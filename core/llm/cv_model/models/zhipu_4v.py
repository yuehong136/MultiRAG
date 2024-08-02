# coding=utf-8
"""
@project: multirag
@Author：龙
@file： core.llm.cv_model.models.zhipu_4v.py.py
@date：2024/7/22 9:38
@desc:
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

from zhipuai import ZhipuAI

from core.llm.cv_model.base import Base
from core.nlp import is_english


@dataclass
class Zhipu4V(Base):
    key: str
    model_name: str = "glm-4v"
    lang: str = "Chinese"
    client: ZhipuAI = field(init=False)

    def __post_init__(self):
        self.client = ZhipuAI(api_key=self.key)

    def describe(self, image: bytes, max_tokens: int = 1024) -> Tuple[str, int]:
        b64 = self.image2base64(image)
        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.prompt(b64),
            max_tokens=max_tokens,
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens

    def chat(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any], image: str = "") -> Tuple[str, int]:
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]
        try:
            for his in history:
                if his["role"] == "user":
                    his["content"] = self.chat_prompt(his["content"], image)

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                max_tokens=gen_conf.get("max_tokens", 1000),
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7)
            )
            return response.choices[0].message.content.strip(), response.usage.total_tokens
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any], image: str = ""):
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        ans = ""
        tk_count = 0
        try:
            for his in history:
                if his["role"] == "user":
                    his["content"] = self.chat_prompt(his["content"], image)

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                max_tokens=gen_conf.get("max_tokens", 1000),
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