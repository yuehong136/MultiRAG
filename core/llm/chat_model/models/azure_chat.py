import json
from openai.lib.azure import AzureOpenAI

from core.llm.chat_model.base import Base


class AzureChat(Base):
    def __init__(self, key, model_name, base_url, **kwargs):
        api_key = json.loads(key).get("api_key", "")
        api_version = json.loads(key).get("api_version", "2024-02-01")
        super().__init__(key, model_name, base_url, **kwargs)
        self.client = AzureOpenAI(api_key=api_key, azure_endpoint=base_url, api_version=api_version)
        self.model_name = model_name