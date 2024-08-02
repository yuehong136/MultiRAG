# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from openai import OpenAI
from core.llm.cv_model.base import Base
from core.nlp import is_english


@dataclass
class GptV4(Base):
    key: str
    model_name: str = "gpt-4o"
    lang: str = "Chinese"
    base_url: str = "https://api.openai.com/v1"
    client: OpenAI = field(init=False)

    def __post_init__(self):
        if not self.base_url:
            self.base_url = "https://api.openai.com/v1"
        self.client = OpenAI(api_key=self.key, base_url=self.base_url)

    def describe(self, image: bytes, max_tokens: int = 300) -> Tuple[str, int]:
        b64 = self.image2base64(image)
        prompt = self.prompt(b64)
        for i in range(len(prompt)):
            for c in prompt[i]["content"]:
                if "text" in c:
                    c["type"] = "text"

        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=prompt,
            max_tokens=max_tokens,
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens
