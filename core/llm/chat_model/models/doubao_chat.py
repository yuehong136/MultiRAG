# doubao.py
from volcenginesdkarkruntime import Ark
from core.llm.chat_model.base import Base


class DoubaoChat(Base):
    def __init__(self, key, model_name, base_url):
        super().__init__(key, model_name, base_url)
        self.client = Ark(api_key=key, base_url=base_url)
        print(f"Doubao client initialized with key: {key}")

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
        total_tokens = 0
        # try:
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
        # except Exception as e:
        #     yield f"**ERROR**: {str(e)}"

        # yield total_tokens
