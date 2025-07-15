from copy import deepcopy

from core.llm.chat_model.base import Base


class GeminiChat(Base):
    def __init__(self, key, model_name, base_url=None, **kwargs):
        super().__init__(key, model_name, base_url=base_url, **kwargs)

        from google.generativeai import GenerativeModel, client

        client.configure(api_key=key)
        _client = client.get_default_generative_client()
        self.model_name = "models/" + model_name
        self.model = GenerativeModel(model_name=self.model_name)
        self.model._client = _client

    def _clean_conf(self, gen_conf):
        for k in list(gen_conf.keys()):
            if k not in ["temperature", "top_p"]:
                del gen_conf[k]
        return gen_conf

    def _chat(self, history, gen_conf):
        from google.generativeai.types import content_types
        system = history[0]["content"] if history and history[0]["role"] == "system" else ""
        hist = []
        for item in history:
            if item["role"] == "system":
                continue
            hist.append(deepcopy(item))
            item = hist[-1]
            if "role" in item and item["role"] == "assistant":
                item["role"] = "model"
            if "role" in item and item["role"] == "system":
                item["role"] = "user"
            if "content" in item:
                item["parts"] = item.pop("content")

        if system:
            self.model._system_instruction = content_types.to_content(system)
        response = self.model.generate_content(hist, generation_config=gen_conf)
        ans = response.text
        return ans, response.usage_metadata.total_token_count

    def chat_streamly(self, system, history, gen_conf):
        from google.generativeai.types import content_types

        if system:
            self.model._system_instruction = content_types.to_content(system)
        for k in list(gen_conf.keys()):
            if k not in ["temperature", "top_p", "max_tokens"]:
                del gen_conf[k]
        for item in history:
            if "role" in item and item["role"] == "assistant":
                item["role"] = "model"
            if "content" in item:
                item["parts"] = item.pop("content")
        ans = ""
        try:
            response = self.model.generate_content(history, generation_config=gen_conf, stream=True)
            for resp in response:
                ans = resp.text
                yield ans

            yield response._chunks[-1].usage_metadata.total_token_count
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield 0
