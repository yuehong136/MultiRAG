from common.token_utils import num_tokens_from_string
from core.llm.cv_model.base import Base
from core.nlp import is_english
from core.prompts.generator import vision_llm_describe_prompt


class AnthropicCV(Base):
    _FACTORY_NAME = "Anthropic"

    def __init__(self, key, model_name, base_url=None, **kwargs):
        import anthropic

        self.client = anthropic.Anthropic(api_key=key)
        self.model_name = model_name
        self.system = ""
        self.max_tokens = 8192
        if "haiku" in self.model_name or "opus" in self.model_name:
            self.max_tokens = 4096
        Base.__init__(self, **kwargs)

    def _image_prompt(self, text, images):
        if not images:
            return text
        pmpt = [{"type": "text", "text": text}]
        for img in images:
            pmpt.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg" if img[:4] != "data" else img.split(":")[1].split(";")[0],
                            "data": img if img[:4] != "data" else img.split(",")[1]
                        },
                    }
            )
        return pmpt

    def describe(self, image):
        b64 = self.image2base64(image)
        response = self.client.messages.create(model=self.model_name, max_tokens=self.max_tokens, messages=self.prompt(b64))
        return response["content"][0]["text"].strip(), response["usage"]["input_tokens"] + response["usage"]["output_tokens"]

    def describe_with_prompt(self, image, prompt=None):
        b64 = self.image2base64(image)
        prompt = self.prompt(b64, prompt if prompt else vision_llm_describe_prompt())

        response = self.client.messages.create(model=self.model_name, max_tokens=self.max_tokens, messages=prompt)
        return response["content"][0]["text"].strip(), response["usage"]["input_tokens"] + response["usage"]["output_tokens"]

    def _clean_conf(self, gen_conf):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        if "max_token" in gen_conf:
            gen_conf["max_tokens"] = self.max_tokens
        return gen_conf

    def chat(self, system, history, gen_conf, images=[]):
        gen_conf = self._clean_conf(gen_conf)
        ans = ""
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=self._form_history(system, history, images),
                system=system,
                stream=False,
                **gen_conf,
            ).to_dict()
            ans = response["content"][0]["text"]
            if response["stop_reason"] == "max_tokens":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            return (
                ans,
                response["usage"]["input_tokens"] + response["usage"]["output_tokens"],
            )
        except Exception as e:
            return ans + "\n**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf, images=[]):
        gen_conf = self._clean_conf(gen_conf)
        total_tokens = 0
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=self._form_history(system, history, images),
                system=system,
                stream=True,
                **gen_conf,
            )
            think = False
            for res in response:
                if res.type == "content_block_delta":
                    if res.delta.type == "thinking_delta" and res.delta.thinking:
                        if not think:
                            yield "<think>"
                            think = True
                        yield res.delta.thinking
                        total_tokens += num_tokens_from_string(res.delta.thinking)
                    elif think:
                        yield "</think>"
                    else:
                        yield res.delta.text
                        total_tokens += num_tokens_from_string(res.delta.text)
        except Exception as e:
            yield "\n**ERROR**: " + str(e)

        yield total_tokens
