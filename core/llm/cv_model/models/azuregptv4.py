import json

from openai.lib.azure import AzureOpenAI

from core.llm.cv_model.models.gptv4 import GptV4
from core.llm.cv_model.base import Base


class AzureGptV4(GptV4):
    _FACTORY_NAME = "Azure-OpenAI"

    def __init__(self, key, model_name, lang="Chinese", **kwargs):
        api_key = json.loads(key).get('api_key', '')
        api_version = json.loads(key).get('api_version', '2024-02-01')
        self.client = AzureOpenAI(api_key=api_key, azure_endpoint=kwargs["base_url"], api_version=api_version)
        self.model_name = model_name
        self.lang = lang
        Base.__init__(self, **kwargs)

    def describe(self, image):
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

    def describe_with_prompt(self, image, prompt=None):
        b64 = self.image2base64(image)
        vision_prompt = self.vision_llm_prompt(b64, prompt) if prompt else self.vision_llm_prompt(b64)

        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=vision_prompt,
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens