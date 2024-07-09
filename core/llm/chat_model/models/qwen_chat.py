# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from dataclasses import dataclass, field
from typing import Any, Tuple
from core.nlp import is_english
from core.llm.chat_model.base import Base
import dashscope


@dataclass
class QWenChat(Base):
    key: str
    model_name: str = "qwen_turbo"
    client: Any = field(init=False)

    def __post_init__(self):
        import dashscope
        dashscope.api_key = self.key
        self.client = dashscope

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> Tuple[str, int]:
        from http import HTTPStatus
        if system:
            history.insert(0, {"role": "system", "content": system})
        response = self.client.Generation.call(
            self.model_name,
            messages=history,
            result_format='message',
            **gen_conf
        )
        ans = ""
        tk_count = 0
        if response.status_code == HTTPStatus.OK:
            ans += response.output.choices[0]['message']['content']
            tk_count += response.usage.total_tokens
            if response.output.choices[0].get("finish_reason", "") == "length":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                    [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            return ans, tk_count

        return "**错误**: " + response.message, tk_count

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        from http import HTTPStatus
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        tk_count = 0
        try:
            response = self.client.Generation.call(
                self.model_name,
                messages=history,
                result_format='message',
                stream=True,
                **gen_conf
            )
            for resp in response:
                if resp.status_code == HTTPStatus.OK:
                    ans = resp.output.choices[0]['message']['content']
                    tk_count = resp.usage.total_tokens
                    if resp.output.choices[0].get("finish_reason", "") == "length":
                        ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                            [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                    yield ans
                else:
                    yield ans + "\n**错误**: " + resp.message if str(resp.message).find("Access")<0 else "余额不足。请在 **settings > Model providers** 中设置 API 密钥。"
        except Exception as e:
            yield ans + "\n**错误**: " + str(e)

        # yield tk_count
