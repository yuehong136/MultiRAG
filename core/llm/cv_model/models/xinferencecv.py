import os

from openai import OpenAI

from core.llm.cv_model.base import Base


class XinferenceCV(Base):
    def __init__(self, key, model_name="", lang="Chinese", base_url=""):
        if base_url.split("/")[-1] != "v1":
            base_url = os.path.join(base_url, "v1")
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name
        self.lang = lang

    def describe(self, image, max_tokens=300):
        b64 = self.image2base64(image)

        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.prompt(b64)
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens