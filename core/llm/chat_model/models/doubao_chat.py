# doubao.py
from dataclasses import dataclass, field
from typing import Any
from volcenginesdkarkruntime import Ark
from core.llm.chat_model.base import Base

@dataclass
class DoubaoChat(Base):
    key: str
    model_name: str
    base_url: str | None = 'https://ark.cn-beijing.volces.com/api/v3'
    client: Ark = field(init=False)

    def __post_init__(self):
        self.client = Ark(api_key=self.key, base_url=self.base_url)
        super().__post_init__()
        print(f"Doubao client initialized with key: {self.key}")

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        # try:
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=history,
            **gen_conf
        )
        ans = response.choices[0].message.content.strip()
        return ans, response.usage.total_tokens
        # except Exception as e:
        #     return f"**ERROR**: {str(e)}", 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        ans = ""
        total_tokens = 0
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                stream=True,
                **gen_conf
            )
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    ans += chunk.choices[0].delta.content
                    total_tokens += 1
                    yield ans
        except Exception as e:
            yield ans + f"\n**ERROR**: {str(e)}"
        yield total_tokens