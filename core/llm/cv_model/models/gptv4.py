# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from dataclasses import dataclass, field
from openai import OpenAI
from core.llm.cv_model.base import Base


class GptV4(Base):
    def __init__(self, key, model_name="gpt-4o", lang="Chinese", base_url="https://api.openai.com/v1"):
        if not base_url:
            base_url="https://api.openai.com/v1"
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang

    def describe(self, image: bytes, max_tokens: int = 300) -> tuple[str, int]:
        b64 = self.image2base64(image)
        prompt = self.prompt(b64)
        for i in range(len(prompt)):
            for c in prompt[i]["content"]:
                if "text" in c:
                    c["type"] = "text"

        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=prompt
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens

if __name__ == '__main__':
    api_key = ""
    image_path = "timestamp0.png"  # 替换为您想描述的图片路径
    # 读取图片文件为字节格式
    with open(image_path, "rb") as img_file:
        image_data = img_file.read()
    gpt_v4 = GptV4(key=api_key)
    description, token_usage = gpt_v4.describe(image_data)
    print("描述：", description)
    print("使用的 tokens 数量：", token_usage)