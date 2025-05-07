from core.llm.cv_model.base import Base
from core.nlp import is_english
from core.prompts import vision_llm_describe_prompt
from core.utils import num_tokens_from_string


class AnthropicCV(Base):
    def __init__(self, key, model_name, base_url=None):
        import anthropic

        self.client = anthropic.Anthropic(api_key=key)
        self.model_name = model_name
        self.system = ""
        self.max_tokens = 8192
        if "haiku" in self.model_name or "opus" in self.model_name:
            self.max_tokens = 4096

    def prompt(self, b64, prompt):
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ],
            }
        ]

    def describe(self, image):
        b64 = self.image2base64(image)
        prompt = self.prompt(b64,
                             "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。" if self.lang.lower() == "chinese" else
                             "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out."
                             )

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            messages=prompt
        )
        return response["content"][0]["text"].strip(), response["usage"]["input_tokens"]+response["usage"]["output_tokens"]

    def describe_with_prompt(self, image, prompt=None):
        b64 = self.image2base64(image)
        prompt = self.prompt(b64, prompt if prompt else vision_llm_describe_prompt())

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            messages=prompt
        )
        return response["content"][0]["text"].strip(), response["usage"]["input_tokens"]+response["usage"]["output_tokens"]

    def chat(self, system, history, gen_conf):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        gen_conf["max_tokens"] = self.max_tokens

        ans = ""
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=history,
                system=system,
                stream=False,
                **gen_conf,
            ).to_dict()
            ans = response["content"][0]["text"]
            if response["stop_reason"] == "max_tokens":
                ans += (
                    "...\nFor the content length reason, it stopped, continue?"
                    if is_english([ans])
                    else "······\n由于长度的原因，回答被截断了，要继续吗？"
                )
            return (
                ans,
                response["usage"]["input_tokens"] + response["usage"]["output_tokens"],
            )
        except Exception as e:
            return ans + "\n**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        gen_conf["max_tokens"] = self.max_tokens

        ans = ""
        total_tokens = 0
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=history,
                system=system,
                stream=True,
                **gen_conf,
            )
            for res in response:
                if res.type == 'content_block_delta':
                    if res.delta.type == "thinking_delta" and res.delta.thinking:
                        if ans.find("<think>") < 0:
                            ans += "<think>"
                        ans = ans.replace("</think>", "")
                        ans += res.delta.thinking + "</think>"
                    else:
                        text = res.delta.text
                        ans += text
                        total_tokens += num_tokens_from_string(text)
                    yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens