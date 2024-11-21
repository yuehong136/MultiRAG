# ollama_chat.py
from dataclasses import dataclass, field
from typing import Any
from ollama import Client


# @dataclass
# class OllamaChat(Base):
#     key: str
#     model_name: str
#     base_url: Optional[str] = None
#     client: Client = field(init=False)
#
#     def __post_init__(self):
#         self.client = Client(host=self.base_url)
#         super().__post_init__()
#
#     def chat(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]) -> Tuple[str, int]:
#         if system:
#             history.insert(0, {"role": "system", "content": system})
#
#         options = {
#             "temperature": gen_conf.get("temperature"),
#             "num_predict": gen_conf.get("max_tokens"),
#             "top_k": gen_conf.get("top_p"),
#             "presence_penalty": gen_conf.get("presence_penalty"),
#             "frequency_penalty": gen_conf.get("frequency_penalty")
#         }
#         options = {k: v for k, v in options.items() if v is not None}
#
#         # try:
#         response = self.client.chat(
#             model=self.model_name,
#             messages=history,
#             options=options,
#             keep_alive=-1
#         )
#         ans = response["message"]["content"].strip()
#         return ans, response["eval_count"] + response.get("prompt_eval_count", 0)
#         # except Exception as e:
#         #     return f"**ERROR**: {str(e)}", 0
#
#     def chat_streamly(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]):
#         if system:
#             history.insert(0, {"role": "system", "content": system})
#
#         options = {
#             "temperature": gen_conf.get("temperature"),
#             "num_predict": gen_conf.get("max_tokens"),
#             "top_k": gen_conf.get("top_p"),
#             "presence_penalty": gen_conf.get("presence_penalty"),
#             "frequency_penalty": gen_conf.get("frequency_penalty")
#         }
#         options = {k: v for k, v in options.items() if v is not None}
#
#         ans = ""
#         # try:
#         response = self.client.chat(
#             model=self.model_name,
#             messages=history,
#             stream=True,
#             options=options,
#             keep_alive=-1
#         )
#         for resp in response:
#             if resp["done"]:
#                 yield resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0)
#             ans += resp["message"]["content"]
#             yield ans
#         # except Exception as e:
#         #     yield ans + f"\n**ERROR**: {str(e)}"
#         # yield 0

@dataclass
class OllamaChat:
    key: str
    model_name: str
    base_url: str = field(default="http://127.0.0.1:11434")  # 可以为 base_url 设置默认值
    client: Client = field(init=False)  # client 会在 __post_init__ 中初始化

    def __post_init__(self):
        # 初始化 client
        self.client = Client(host=self.base_url)

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        try:
            options = {}
            if "temperature" in gen_conf:
                options["temperature"] = gen_conf["temperature"]
            if "max_tokens" in gen_conf:
                options["num_predict"] = gen_conf["max_tokens"]
            if "top_p" in gen_conf:
                options["top_k"] = gen_conf["top_p"]
            if "presence_penalty" in gen_conf:
                options["presence_penalty"] = gen_conf["presence_penalty"]
            if "frequency_penalty" in gen_conf:
                options["frequency_penalty"] = gen_conf["frequency_penalty"]

            response = self.client.chat(
                model=self.model_name,
                messages=history,
                options=options,
                keep_alive=-1
            )
            ans = response["message"]["content"].strip()
            return ans, response["eval_count"] + response.get("prompt_eval_count", 0)
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]):
        if system:
            history.insert(0, {"role": "system", "content": system})
        options = {}
        if "temperature" in gen_conf:
            options["temperature"] = gen_conf["temperature"]
        if "max_tokens" in gen_conf:
            options["num_predict"] = gen_conf["max_tokens"]
        if "top_p" in gen_conf:
            options["top_k"] = gen_conf["top_p"]
        if "presence_penalty" in gen_conf:
            options["presence_penalty"] = gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            options["frequency_penalty"] = gen_conf["frequency_penalty"]

        ans = ""
        try:
            response = self.client.chat(
                model=self.model_name,
                messages=history,
                stream=True,
                options=options,
                keep_alive=-1
            )
            for resp in response:
                if resp["done"]:
                    yield resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0)
                ans += resp["message"]["content"]
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)
        yield 0