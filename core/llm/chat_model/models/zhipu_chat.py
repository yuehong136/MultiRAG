import json
import logging

from zhipuai import ZhipuAI

from core.llm.chat_model.base import LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN, Base
from core.nlp import is_chinese


class ZhipuChat(Base):
    _FACTORY_NAME = "ZHIPU-AI"

    def __init__(self, key, model_name="glm-4-airx", base_url=None, **kwargs):
        super().__init__(key, model_name, base_url=base_url, **kwargs)

        self.client = ZhipuAI(api_key=key)
        self.model_name = model_name

    def _clean_conf(self, gen_conf):
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        gen_conf = self._clean_conf_plealty(gen_conf)
        return gen_conf

    def _clean_conf_plealty(self, gen_conf):
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        return gen_conf

    def chat_with_tools(self, system: str, history: list, gen_conf: dict):
        gen_conf = self._clean_conf_plealty(gen_conf)

        return super().chat_with_tools(system, history, gen_conf)

    def chat_streamly(self, system, history, gen_conf={}, **kwargs):
        if system and history and history[0].get("role") != "system":
            history.insert(0, {"role": "system", "content": system})
        gen_conf = self._clean_conf(gen_conf)
        ans = ""
        tk_count = 0
        try:
            logging.info(json.dumps(history, ensure_ascii=False, indent=2))
            response = self.client.chat.completions.create(model=self.model_name, messages=history, stream=True, **gen_conf)
            for resp in response:
                if not resp.choices[0].delta.content:
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
        gen_conf = self._clean_conf_plealty(gen_conf)
        return super().chat_streamly_with_tools(system, history, gen_conf)
