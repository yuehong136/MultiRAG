import base64
from io import BytesIO

from core.llm.cv_model.base import Base
from core.prompts.prompts import vision_llm_describe_prompt


class GeminiCV(Base):
    _FACTORY_NAME = "Gemini"

    def __init__(self, key, model_name="gemini-1.0-pro-vision-latest", lang="Chinese", **kwargs):
        from google.generativeai import GenerativeModel, client

        client.configure(api_key=key)
        _client = client.get_default_generative_client()
        self.model_name = model_name
        self.model = GenerativeModel(model_name=self.model_name)
        self.model._client = _client
        self.lang = lang
        Base.__init__(self, **kwargs)

    def _form_history(self, system, history, images=None):
        if images is None:
            images = []
        hist = []
        if system:
            hist.append({"role": "user", "parts": [system, history[0]["content"]]})
        for img in images:
            hist[0]["parts"].append(("data:image/jpeg;base64," + img) if img[:4]!="data" else img)
        for h in history[1:]:
            hist.append({"role": "user" if h["role"]=="user" else "model", "parts": [h["content"]]})
        return hist

    def describe(self, image):
        from PIL.Image import open

        prompt = (
            "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。"
            if self.lang.lower() == "chinese"
            else "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out."
        )
        b64 = self.image2base64(image)
        img = open(BytesIO(base64.b64decode(b64)))
        input = [prompt, img]
        res = self.model.generate_content(input)
        return res.text, res.usage_metadata.total_token_count

    def describe_with_prompt(self, image, prompt=None):
        from PIL.Image import open

        b64 = self.image2base64(image)
        vision_prompt = prompt if prompt else vision_llm_describe_prompt()
        img = open(BytesIO(base64.b64decode(b64)))
        input = [vision_prompt, img]
        res = self.model.generate_content(
            input,
        )
        return res.text, res.usage_metadata.total_token_count

    def chat(self, system, history, gen_conf, images=None):
        if images is None:
            images = []
        generation_config = dict(temperature=gen_conf.get("temperature", 0.3), top_p=gen_conf.get("top_p", 0.7))
        try:
            response = self.model.generate_content(
                self._form_history(system, history, images),
                generation_config=generation_config)
            ans = response.text
            return ans, response.usage_metadata.total_token_count
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf, images=None):
        if images is None:
            images = []
        ans = ""
        response = None
        try:
            generation_config = dict(temperature=gen_conf.get("temperature", 0.3), top_p=gen_conf.get("top_p", 0.7))
            response = self.model.generate_content(
                self._form_history(system, history, images),
                generation_config=generation_config,
                stream=True,
            )

            for resp in response:
                if not resp.text:
                    continue
                ans = resp.text
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        if response and hasattr(response, "usage_metadata") and hasattr(response.usage_metadata, "total_token_count"):
            yield response.usage_metadata.total_token_count
        else:
            yield 0