"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from openai import OpenAI

from core.llm.cv_model.base import Base


class GptV4(Base):
    _FACTORY_NAME = "OpenAI"

    def __init__(self, key, model_name="gpt-4o", lang="Chinese", base_url="https://api.openai.com/v1", **kwargs):
        if not base_url:
            base_url = "https://api.openai.com/v1"
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang
        super().__init__(**kwargs)

    def describe(self, image):
        b64 = self.image2base64(image)
        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.prompt(b64),
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens

    def describe_with_prompt(self, image, prompt=None):
        b64 = self.image2base64(image)
        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.vision_llm_prompt(b64, prompt),
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens
