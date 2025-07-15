# zhipu_chat.py
from typing import Any
from core.llm.chat_model.base import Base, LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN
from zhipuai import ZhipuAI

from core.nlp import is_chinese, is_english


class ZhipuChat(Base):
    def __init__(self, key, model_name="glm-4-airx", base_url=None, **kwargs):
        super().__init__(key, model_name, base_url=base_url, **kwargs)

        self.client = ZhipuAI(api_key=key)
        self.model_name = model_name

    def _clean_conf(self, gen_conf):
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        return gen_conf

    def chat_with_tools(self, system: str, history: list, gen_conf: dict):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]

        return super().chat_with_tools(system, history, gen_conf)

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        ans = ""
        tk_count = 0
        try:
            response = self.client.chat.completions.create(model=self.model_name, messages=history, stream=True, **gen_conf)
            for resp in response:
                if not resp.choices[0].delta.content and resp.choices[0].finish_reason != 'stop':
                    continue
                delta = resp.choices[0].delta.content
                ans = delta
                if resp.choices[0].finish_reason == "length":
                    if is_chinese(ans):
                        ans += LENGTH_NOTIFICATION_CN
                    else:
                        ans += LENGTH_NOTIFICATION_EN
                    tk_count = self.total_token_count(resp)
                if resp.choices[0].finish_reason == "stop":
                    tk_count = self.total_token_count(resp)
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count

    def chat_streamly_with_tools(self, system: str, history: list, gen_conf: dict):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]

        return super().chat_streamly_with_tools(system, history, gen_conf)


    async def achat_streamly(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=history,
            stream=True,
            **gen_conf
        )
        for chunk in response:
            if hasattr(chunk.choices[0].delta, 'content'):
                ans += chunk.choices[0].delta.content
                yield ans
