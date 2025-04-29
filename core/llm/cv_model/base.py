# coding=utf-8
"""
@project: multirag
@Author：龙
@file： core.llm.cv_model.base.py
@date：2024/7/22 9:30
@desc:
"""
from abc import ABC
import openai
from core.nlp import is_english
from typing import Any
import base64
from io import BytesIO

from core.prompts import vision_llm_describe_prompt


class Base(ABC):
    def __init__(self, key, model_name):
        pass

    def describe(self, image):
        raise NotImplementedError("Please implement encode method!")

    def describe_with_prompt(self, image, prompt=None):
        raise NotImplementedError("Please implement encode method!")

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], image: str = "") -> tuple[str, int]:
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]
        try:
            for his in history:
                if his["role"] == "user":
                    his["content"] = self.chat_prompt(his["content"], image)

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7)
            )
            return response.choices[0].message.content.strip(), response.usage.total_tokens
        except openai.APIError as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], image: str = ""):
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        ans = ""
        total_tokens = 0
        try:
            for his in history:
                if his["role"] == "user":
                    his["content"] = self.chat_prompt(his["content"], image)

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7),
                stream=True
            )
            for resp in response:
                if not resp.choices or not resp.choices[0].delta.content:
                    continue
                delta = resp.choices[0].delta.content
                ans += delta
                if resp.choices[0].finish_reason == "length":
                    ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                if resp.choices[0].finish_reason == "stop":
                    total_tokens = resp.usage.total_tokens
                yield ans
        except openai.APIError as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens

    def image2base64(self, image: Any) -> str:
        if isinstance(image, bytes):
            return base64.b64encode(image).decode("utf-8")
        if isinstance(image, BytesIO):
            return base64.b64encode(image.getvalue()).decode("utf-8")
        buffered = BytesIO()
        try:
            image.save(buffered, format="JPEG")
        except Exception:
            image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")

    def prompt(self, b64: str) -> list[dict[str, Any]]:
        print(f"Prompt called with lang={self.lang}")  # 调试信息
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。" if self.lang.lower() == "chinese" else
                        "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out.",
                    },
                ],
            }
        ]

    def vision_llm_prompt(self, b64, prompt=None):
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt if prompt else vision_llm_describe_prompt(),
                    },
                ],
            }
        ]

    def chat_prompt(self, text: str, b64: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                },
            },
            {
                "type": "text",
                "text": text
            },
        ]

    def chat_onlytext(self, text: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "text",
                "text": text
            },
        ]
