import os

from openai import OpenAI

from core.llm import GptV4


class OpenAI_APICV(GptV4):
    def __init__(self, key, model_name, lang="Chinese", base_url=""):
        if not base_url:
            raise ValueError("url cannot be None")
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name.split("___")[0]
        self.lang = lang