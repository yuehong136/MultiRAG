# zhipu_chat.py
from dataclasses import dataclass, field
from typing import Any
from core.llm.chat_model.base import Base, LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN
from zhipuai import ZhipuAI

from core.nlp import is_chinese, is_english


@dataclass
class ZhipuChat(Base):
    key: str
    model_name: str = "glm-4-plus"
    base_url: str | None = None
    client: ZhipuAI = field(init=False)

    def __post_init__(self):
        self.client = ZhipuAI(api_key=self.key)
        # print(f"ZhipuAI client initialized with key: {self.key}")

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                **gen_conf
            )
            ans = response.choices[0].message.content.strip()
            if response.choices[0].finish_reason == "length":
                if is_chinese(ans):
                    ans += LENGTH_NOTIFICATION_CN
                else:
                    ans += LENGTH_NOTIFICATION_EN
            return ans, self.total_token_count(response)
        except Exception as e:
            return f"**ERROR**: {str(e)}", 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "presence_penalty" in gen_conf:
            del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            del gen_conf["frequency_penalty"]
        ans = ""
        tk_count = 0
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                stream=True,
                **gen_conf
            )
            for resp in response:
                if not resp.choices[0].delta.content and resp.choices[0].finish_reason != 'stop':
                    continue
                delta = resp.choices[0].delta.content
                ans += delta
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