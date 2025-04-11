import os

from openai import OpenAI

from core.llm import OpenAIEmbed


class GPUStackEmbed(OpenAIEmbed):
    def __init__(self, key, model_name, base_url):
        if not base_url:
            raise ValueError("url cannot be None")
        if base_url.split("/")[-1] != "v1-openai":
            base_url = os.path.join(base_url, "v1-openai")

        print(key,base_url)
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name