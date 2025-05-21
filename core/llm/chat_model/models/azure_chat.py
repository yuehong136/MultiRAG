import json
from openai.lib.azure import AzureOpenAI

from core.llm.chat_model.base import Base


class AzureChat(Base):
    def __init__(self, key, model_name, **kwargs):
        api_key = json.loads(key).get("api_key", "")
        api_version = json.loads(key).get("api_version", "2024-02-01")
        super().__init__(key, model_name, kwargs["base_url"])
        self.client = AzureOpenAI(api_key=api_key, azure_endpoint=kwargs["base_url"], api_version=api_version)
        self.model_name = model_name