import os
from abc import ABC, abstractmethod

from openai import OpenAI


class LLM(ABC):
    def __init__(self, api_key, model="gpt-3.5-turbo", base_url=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def generate(self, prompt, **kwargs):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs
        )
        return response.choices[0].message.content

    def generate_stream(self, prompt, **kwargs):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            **kwargs
        )
        # for chunk in response:
        #     if not chunk.choices:
        #         continue
        #     print(chunk.choices[0].delta.content, end="")
        return response


class OpenAILLM(LLM):
    pass  # 完全使用基类的实现


# 示例用法
if __name__ == "__main__":
    # 使用OpenAI LLM
    api_key = os.getenv("API_KEY")
    # 使用自定义API端点的OpenAI兼容LLM
    custom_llm = OpenAILLM(api_key, model="ep-20240808173556-h7vxq",
                           base_url="https://ark.cn-beijing.volces.com/api/v3")

    # 非流式输出
    response = custom_llm.generate("Tell me a joke")
    print(response)

    stream = custom_llm.generate_stream("给我讲个笑话")
    for chunk in stream:
        if not chunk.choices:
            continue
        print(chunk.choices[0].delta.content, end="")
    print()
