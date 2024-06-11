# base.py
from abc import ABC, abstractmethod

import openai
from openai import OpenAI

from core.nlp import is_english


class Base(ABC):
    def __init__(self, key, model_name, base_url=None):
        self.model_name = model_name
        self.client = OpenAI(api_key=key, base_url=base_url)
    # @abstractmethod
    def chat(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                **gen_conf)
            ans = response.choices[0].message.content.strip()
            if response.choices[0].finish_reason == "length":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                    [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            return ans, response.usage.total_tokens
        except openai.APIError as e:
            return "**ERROR**: " + str(e), 0

    # @abstractmethod
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
            **gen_conf)
        for resp in response:
            if not resp.choices or not resp.choices[0].delta.content: continue
            ans += resp.choices[0].delta.content
            total_tokens += 1
            if resp.choices[0].finish_reason == "length":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                    [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            yield ans

        # except openai.APIError as e:
        #     yield ans + "\n**ERROR**: " + str(e)

        # yield total_tokens
