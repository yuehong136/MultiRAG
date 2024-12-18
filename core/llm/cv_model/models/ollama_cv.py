from ollama import Client

from core.llm.cv_model.base import Base


class OllamaCV(Base):
    def __init__(self, key, model_name, lang="Chinese", **kwargs):
        self.client = Client(host=kwargs["base_url"])
        self.model_name = model_name
        self.lang = lang

    def describe(self, image, max_tokens=1024):
        prompt = self.prompt("")
        try:
            options = {"num_predict": max_tokens}
            response = self.client.generate(
                model=self.model_name,
                prompt=prompt[0]["content"][1]["text"],
                images=[image],
                options=options
            )
            ans = response["response"].strip()
            return ans, 128
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat(self, system, history, gen_conf, image=""):
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        try:
            for his in history:
                if his["role"] == "user":
                    his["images"] = [image]
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

    def chat_streamly(self, system, history, gen_conf, image=""):
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                his["images"] = [image]
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