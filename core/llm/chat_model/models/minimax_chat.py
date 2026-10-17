import json
from collections.abc import Iterator
from typing import Any

import requests

from common.token_utils import num_tokens_from_string
from core.llm.chat_model.base import LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN, Base
from core.llm.chat_model.minimax_auth import decode_minimax_key, minimax_request_options
from core.nlp import is_chinese


class MiniMaxChat(Base):
    def __init__(self, key: str | dict[str, Any], model_name: str, base_url: str | None = "https://api.minimax.chat/v1/text/chatcompletion_v2", **kwargs: Any) -> None:
        api_key, group_id = decode_minimax_key(key)
        self.base_url, self.query = minimax_request_options(base_url or "https://api.minimax.chat/v1/text/chatcompletion_v2", group_id)
        self.extra_headers = dict(kwargs.get("extra_headers") or {})
        super().__init__(api_key, model_name, base_url=self.base_url, **kwargs)
        self.client = self.client.with_options(default_query=self.query, default_headers=self.extra_headers)
        self.model_name = model_name
        self.api_key = api_key

    def _clean_conf(self, gen_conf: dict[str, Any]) -> dict[str, Any]:
        for k in list(gen_conf.keys()):
            if k not in ["temperature", "top_p", "max_tokens"]:
                del gen_conf[k]
        return gen_conf

    def _chat(self, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        payload = json.dumps({"model": self.model_name, "messages": history, **gen_conf})
        response = requests.request("POST", url=self.base_url, params=self.query, headers=headers, data=payload)
        response = response.json()
        ans = response["choices"][0]["message"]["content"].strip()
        if response["choices"][0]["finish_reason"] == "length":
            if is_chinese(ans):
                ans += LENGTH_NOTIFICATION_CN
            else:
                ans += LENGTH_NOTIFICATION_EN
        return ans, self.total_token_count(response)

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> Iterator[str | int]:
        if system:
            history.insert(0, {"role": "system", "content": system})
        for k in list(gen_conf.keys()):
            if k not in ["temperature", "top_p", "max_tokens"]:
                del gen_conf[k]
        ans = ""
        total_tokens = 0
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **self.extra_headers,
            }
            payload = json.dumps(
                {
                    "model": self.model_name,
                    "messages": history,
                    "stream": True,
                    **gen_conf,
                }
            )
            response = requests.request(
                "POST",
                url=self.base_url,
                params=self.query,
                headers=headers,
                data=payload,
            )
            for resp in response.text.split("\n\n")[:-1]:
                resp = json.loads(resp[6:])
                text = ""
                if "choices" in resp and "delta" in resp["choices"][0]:
                    text = resp["choices"][0]["delta"]["content"]
                ans = text
                tol = self.total_token_count(resp)
                if not tol:
                    total_tokens += num_tokens_from_string(text)
                else:
                    total_tokens = tol
                yield ans

        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens
