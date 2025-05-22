from core.llm.chat_model.base import Base
from core.nlp import is_english
from core.utils import num_tokens_from_string


class AnthropicChat(Base):
    def __init__(self, key, model_name, base_url=None):
        super().__init__(key, model_name, base_url=None)

        import anthropic

        self.client = anthropic.Anthropic(api_key=key)
        self.model_name = model_name
        self.system = ""

    def chat(self, system, history, gen_conf):
        if system:
            self.system = system
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        gen_conf["max_tokens"] = 8192
        if "haiku" in self.model_name or "opus" in self.model_name:
            gen_conf["max_tokens"] = 4096

        ans = ""
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=history,
                system=self.system,
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

    def chat_streamly(self, system, history, gen_conf):
        if system:
            self.system = system
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        gen_conf["max_tokens"] = 8192
        if "haiku" in self.model_name or "opus" in self.model_name:
            gen_conf["max_tokens"] = 4096

        ans = ""
        total_tokens = 0
        reasoning_start = False
        try:
            response = self.client.messages.create(
                model=self.model_name,
                messages=history,
                system=system,
                stream=True,
                **gen_conf,
            )
            for res in response:
                if res.type == "content_block_delta":
                    if res.delta.type == "thinking_delta" and res.delta.thinking:
                        ans = ""
                        if not reasoning_start:
                            reasoning_start = True
                            ans = "<think>"
                        ans += res.delta.thinking + "</think>"
                    else:
                        reasoning_start = False
                        text = res.delta.text
                        ans = text
                        total_tokens += num_tokens_from_string(text)
                    yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens