# base.py
import os
from abc import ABC
from dataclasses import dataclass, field
import openai
from openai import OpenAI
from core.nlp import is_chinese, is_english
from typing import Any

from core.utils import num_tokens_from_string

LENGTH_NOTIFICATION_CN = "······\n由于长度的原因，回答被截断了，要继续吗？"
LENGTH_NOTIFICATION_EN = "...\nFor the content length reason, it stopped, continue?"

@dataclass
class Base(ABC):
    key: str
    model_name: str
    base_url: str | None = None
    client: OpenAI = field(init=False)

    def __post_init__(self):
        timeout = int(os.environ.get('LM_TIMEOUT_SECONDS', 600))
        self.client = OpenAI(api_key=self.key, base_url=self.base_url, timeout=timeout)

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        if system:
            history.insert(0, {"role": "system", "content": system})
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
            return ans, response.usage.total_tokens
        except openai.APIError as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        total_tokens = 0
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                stream=True,
                **gen_conf
            )
            for resp in response:
                if not resp.choices: continue
                if not resp.choices[0].delta.content:
                    resp.choices[0].delta.content = ""
                ans += resp.choices[0].delta.content

                if not hasattr(resp, "usage") or not resp.usage:
                    total_tokens = (
                            total_tokens
                            + num_tokens_from_string(resp.choices[0].delta.content)
                    )
                elif isinstance(resp.usage, dict):
                    total_tokens = resp.usage.get("total_tokens", total_tokens)
                else:
                    total_tokens = resp.usage.total_tokens

                if resp.choices[0].finish_reason == "length":
                    if is_chinese(ans):
                        ans += LENGTH_NOTIFICATION_CN
                    else:
                        ans += LENGTH_NOTIFICATION_EN
                yield ans

        except openai.APIError as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens
