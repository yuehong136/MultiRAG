from core.llm.chat_model.base import Base
from core.nlp import is_english
from core.utils import num_tokens_from_string


class CoHereChat(Base):
    def __init__(self, key, model_name, base_url=""):
        from cohere import Client

        self.client = Client(api_key=key)
        self.model_name = model_name

    def chat(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "top_p" in gen_conf:
            gen_conf["p"] = gen_conf.pop("top_p")
        if "frequency_penalty" in gen_conf and "presence_penalty" in gen_conf:
            gen_conf.pop("presence_penalty")
        for item in history:
            if "role" in item and item["role"] == "user":
                item["role"] = "USER"
            if "role" in item and item["role"] == "assistant":
                item["role"] = "CHATBOT"
            if "content" in item:
                item["message"] = item.pop("content")
        mes = history.pop()["message"]
        ans = ""
        try:
            response = self.client.chat(model=self.model_name, chat_history=history, message=mes, **gen_conf)
            ans = response.text
            if response.finish_reason == "MAX_TOKENS":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            return (
                ans,
                response.meta.tokens.input_tokens + response.meta.tokens.output_tokens,
            )
        except Exception as e:
            return ans + "\n**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "top_p" in gen_conf:
            gen_conf["p"] = gen_conf.pop("top_p")
        if "frequency_penalty" in gen_conf and "presence_penalty" in gen_conf:
            gen_conf.pop("presence_penalty")
        for item in history:
            if "role" in item and item["role"] == "user":
                item["role"] = "USER"
            if "role" in item and item["role"] == "assistant":
                item["role"] = "CHATBOT"
            if "content" in item:
                item["message"] = item.pop("content")
        mes = history.pop()["message"]
        ans = ""
        total_tokens = 0
        try:
            response = self.client.chat_stream(model=self.model_name, chat_history=history, message=mes, **gen_conf)
            for resp in response:
                if resp.event_type == "text-generation":
                    ans = resp.text
                    total_tokens += num_tokens_from_string(resp.text)
                elif resp.event_type == "stream-end":
                    if resp.finish_reason == "MAX_TOKENS":
                        ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                yield ans

        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens