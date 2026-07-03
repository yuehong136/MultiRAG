from urllib.parse import urljoin

from openai import OpenAI

from core.llm.embedding_model.openai_embed import OpenAIEmbed


class OpenAI_APIEmbed(OpenAIEmbed):
    def __init__(self, key, model_name, base_url):
        if not base_url:
            raise ValueError("url cannot be None")
        base_url = urljoin(base_url, "v1")
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name.split("___")[0]
