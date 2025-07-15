import json

from core.llm.chat_model.base import Base


class HunyuanChat(Base):
    def __init__(self, key, model_name, base_url=None, **kwargs):
        super().__init__(key, model_name, base_url=base_url, **kwargs)

        from tencentcloud.common import credential
        from tencentcloud.hunyuan.v20230901 import hunyuan_client

        key = json.loads(key)
        sid = key.get("hunyuan_sid", "")
        sk = key.get("hunyuan_sk", "")
        cred = credential.Credential(sid, sk)
        self.model_name = model_name
        self.client = hunyuan_client.HunyuanClient(cred, "")

    def _clean_conf(self, gen_conf):
        _gen_conf = {}
        if "temperature" in gen_conf:
            _gen_conf["Temperature"] = gen_conf["temperature"]
        if "top_p" in gen_conf:
            _gen_conf["TopP"] = gen_conf["top_p"]
        return _gen_conf

    def _chat(self, history, gen_conf):
        from tencentcloud.hunyuan.v20230901 import models

        hist = [{k.capitalize(): v for k, v in item.items()} for item in history]
        req = models.ChatCompletionsRequest()
        params = {"Model": self.model_name, "Messages": hist, **gen_conf}
        req.from_json_string(json.dumps(params))
        response = self.client.ChatCompletions(req)
        ans = response.Choices[0].Message.Content
        return ans, response.Usage.TotalTokens

    def chat_streamly(self, system, history, gen_conf):
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )
        from tencentcloud.hunyuan.v20230901 import models

        _gen_conf = {}
        _history = [{k.capitalize(): v for k, v in item.items()} for item in history]
        if system:
            _history.insert(0, {"Role": "system", "Content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if "temperature" in gen_conf:
            _gen_conf["Temperature"] = gen_conf["temperature"]
        if "top_p" in gen_conf:
            _gen_conf["TopP"] = gen_conf["top_p"]

        req = models.ChatCompletionsRequest()
        params = {
            "Model": self.model_name,
            "Messages": _history,
            "Stream": True,
            **_gen_conf,
        }
        req.from_json_string(json.dumps(params))
        ans = ""
        total_tokens = 0
        try:
            response = self.client.ChatCompletions(req)
            for resp in response:
                resp = json.loads(resp["data"])
                if not resp["Choices"] or not resp["Choices"][0]["Delta"]["Content"]:
                    continue
                ans = resp["Choices"][0]["Delta"]["Content"]
                total_tokens += 1

                yield ans

        except TencentCloudSDKException as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens