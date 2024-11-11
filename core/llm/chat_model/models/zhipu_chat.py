# zhipu_chat.py
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from core.llm.chat_model.base import Base
from zhipuai import ZhipuAI

@dataclass
class ZhipuChat(Base):
    key: str
    model_name: str = "glm-4-0520"
    base_url: Optional[str] = None
    client: ZhipuAI = field(init=False)

    def __post_init__(self):
        self.client = ZhipuAI(api_key=self.key)
        print(f"ZhipuAI client initialized with key: {self.key}")

    def chat(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]) -> Tuple[str, int]:
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "presence_penalty" in gen_conf: del gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf: del gen_conf["frequency_penalty"]
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                **gen_conf
            )
            ans = response.choices[0].message.content.strip()
            return ans, response.usage.total_tokens
        except Exception as e:
            return f"**ERROR**: {str(e)}", 0

    def chat_streamly(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        try:
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
        except Exception as e:
            yield f"**ERROR**: {str(e)}"

    #     # except Exception as e:
    #     #     yield f"**ERROR**: {str(e)}"

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