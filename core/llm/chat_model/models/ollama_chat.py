import os

from ollama import Client

from core.llm.chat_model.base import Base


class OllamaChat(Base):
    def __init__(self, key, model_name, base_url=None, **kwargs):
        super().__init__(key, model_name, base_url=base_url, **kwargs)

        self.client = Client(host=base_url) if not key or key == "x" else Client(host=base_url, headers={"Authorization": f"Bearer {key}"})
        self.model_name = model_name
        self.keep_alive = kwargs.get("ollama_keep_alive", int(os.environ.get("OLLAMA_KEEP_ALIVE", -1)))

    def _clean_conf(self, gen_conf):
        options = {}
        if "max_tokens" in gen_conf:
            options["num_predict"] = gen_conf["max_tokens"]
        for k in ["temperature", "top_p", "presence_penalty", "frequency_penalty"]:
            if k not in gen_conf:
                continue
            options[k] = gen_conf[k]
        return options

    def _chat(self, history, gen_conf):
        # Calculate context size
        ctx_size = self._calculate_dynamic_ctx(history)

        gen_conf["num_ctx"] = ctx_size
        response = self.client.chat(model=self.model_name, messages=history, options=gen_conf, keep_alive=self.keep_alive)
        ans = response["message"]["content"].strip()
        token_count = response.get("eval_count", 0) + response.get("prompt_eval_count", 0)
        return ans, token_count

    def chat_streamly(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        try:
            # Calculate context size
            ctx_size = self._calculate_dynamic_ctx(history)
            options = {"num_ctx": ctx_size}
            if "temperature" in gen_conf:
                options["temperature"] = gen_conf["temperature"]
            if "max_tokens" in gen_conf:
                options["num_predict"] = gen_conf["max_tokens"]
            if "top_p" in gen_conf:
                options["top_p"] = gen_conf["top_p"]
            if "presence_penalty" in gen_conf:
                options["presence_penalty"] = gen_conf["presence_penalty"]
            if "frequency_penalty" in gen_conf:
                options["frequency_penalty"] = gen_conf["frequency_penalty"]

            ans = ""
            try:
                response = self.client.chat(model=self.model_name, messages=history, stream=True, options=options, keep_alive=self.keep_alive)
                for resp in response:
                    if resp["done"]:
                        token_count = resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0)
                        yield token_count
                    ans = resp["message"]["content"]
                    yield ans
            except Exception as e:
                yield ans + "\n**ERROR**: " + str(e)
            yield 0
        except Exception as e:
            yield "**ERROR**: " + str(e)
            yield 0