import json

from core.llm.chat_model.base import Base


class BaiduYiyanChat(Base):
    def __init__(self, key, model_name, base_url="https://qianfan.baidubce.com/v2"):
        if not base_url:
            base_url = "https://qianfan.baidubce.com/v2"
        super().__init__(key, model_name, base_url)
# class BaiduYiyanChat(Base):
#     def __init__(self, key, model_name, base_url=None):
#         import qianfan
#
#         key = json.loads(key)
#         ak = key.get("yiyan_ak", "")
#         sk = key.get("yiyan_sk", "")
#         self.client = qianfan.ChatCompletion()
#         self.model_name = model_name.lower()
#         self.system = ""
#
#     def chat(self, system, history, gen_conf):
#         if system:
#             self.system = system
#         gen_conf["penalty_score"] = ((gen_conf.get("presence_penalty", 0) + gen_conf.get("frequency_penalty", 0)) / 2) + 1
#         if "max_tokens" in gen_conf:
#             del gen_conf["max_tokens"]
#         ans = ""
#
#         try:
#             response = self.client.do(model=self.model_name, messages=history, system=self.system, **gen_conf).body
#             ans = response["result"]
#             return ans, self.total_token_count(response)
#
#         except Exception as e:
#             return ans + "\n**ERROR**: " + str(e), 0
#
#     def chat_streamly(self, system, history, gen_conf):
#         if system:
#             self.system = system
#         gen_conf["penalty_score"] = ((gen_conf.get("presence_penalty", 0) + gen_conf.get("frequency_penalty", 0)) / 2) + 1
#         if "max_tokens" in gen_conf:
#             del gen_conf["max_tokens"]
#         ans = ""
#         total_tokens = 0
#
#         try:
#             response = self.client.do(model=self.model_name, messages=history, system=self.system, stream=True, **gen_conf)
#             for resp in response:
#                 resp = resp.body
#                 ans = resp["result"]
#                 total_tokens = self.total_token_count(resp)
#
#                 yield ans
#
#         except Exception as e:
#             return ans + "\n**ERROR**: " + str(e), 0
#
#         yield total_tokens