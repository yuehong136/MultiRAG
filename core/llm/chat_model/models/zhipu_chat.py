# zhipu_chat.py
import openai

from core.llm.chat_model.base import Base
from zhipuai import ZhipuAI

class ZhipuChat(Base):
    def __init__(self, key, model_name="glm-4", base_url=None):
        super().__init__(key, model_name, base_url)
        self.client = ZhipuAI(api_key=key)
        print(f"ZhipuAI client initialized with key: {key}")

    def chat(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
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

    def chat_streamly(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        # try:
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
    #     # except Exception as e:
    #     #     yield f"**ERROR**: {str(e)}"

    # async def chat_streamly(self, system, history, gen_conf):
    #     if system:
    #         history.insert(0, {"role": "system", "content": system})
    #     ans = ""
    #     response = self.client.chat.completions.create(
    #         model=self.model_name,
    #         messages=history,
    #         stream=True,
    #         **gen_conf
    #     )
    #     for chunk in response:
    #         if hasattr(chunk.choices[0].delta, 'content'):
    #             ans += chunk.choices[0].delta.content
    #             yield ans