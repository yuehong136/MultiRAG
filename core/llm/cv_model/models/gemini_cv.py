import base64
from io import BytesIO
from core.llm.cv_model.base import Base
from core.prompts import vision_llm_describe_prompt


class GeminiCV(Base):
    def __init__(self, key, model_name="gemini-1.5-pro", lang="Chinese", **kwargs):
        from google.generativeai import GenerativeModel, client
        client.configure(api_key=key)
        _client = client.get_default_generative_client()
        self.model_name = model_name
        self.model = GenerativeModel(model_name=self.model_name)
        self.model._client = _client
        self.lang = lang

    def describe(self, image):
        from PIL.Image import open
        prompt = "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。" if self.lang.lower() == "chinese" else \
            "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out."
        b64 = self.image2base64(image)
        img = open(BytesIO(base64.b64decode(b64)))
        input = [prompt, img]
        res = self.model.generate_content(
            input
        )
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

    def chat(self, system, history, gen_conf, image=""):
        from transformers import GenerationConfig
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]
        try:
            for his in history:
                if his["role"] == "assistant":
                    his["role"] = "model"
                    his["parts"] = [his["content"]]
                    his.pop("content")
                if his["role"] == "user":
                    his["parts"] = [his["content"]]
                    his.pop("content")
            history[-1]["parts"].append("data:image/jpeg;base64," + image)

            response = self.model.generate_content(history, generation_config=GenerationConfig(
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7)))

            ans = response.text
            return ans, response.usage_metadata.total_token_count
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf, image=""):
        from transformers import GenerationConfig
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        ans = ""
        try:
            for his in history:
                if his["role"] == "assistant":
                    his["role"] = "model"
                    his["parts"] = [his["content"]]
                    his.pop("content")
                if his["role"] == "user":
                    his["parts"] = [his["content"]]
                    his.pop("content")
            history[-1]["parts"].append("data:image/jpeg;base64," + image)

            response = self.model.generate_content(history, generation_config=GenerationConfig(
                temperature=gen_conf.get("temperature", 0.3),
                top_p=gen_conf.get("top_p", 0.7)), stream=True)

            for resp in response:
                if not resp.text:
                    continue
                ans += resp.text
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield response._chunks[-1].usage_metadata.total_token_count
