import os
from copy import deepcopy

from core.llm.cv_model.base import Base


class OllamaCV(Base):
    _FACTORY_NAME = "Ollama"

    def __init__(self, key, model_name, lang="Chinese", **kwargs):
        from ollama import Client

        self.client = Client(host=kwargs["base_url"])
        self.model_name = model_name
        self.lang = lang
        self.keep_alive = kwargs.get("ollama_keep_alive", int(os.environ.get("OLLAMA_KEEP_ALIVE", -1)))
        Base.__init__(self, **kwargs)

    def _clean_img(self, img):
        if not isinstance(img, str):
            return img

        # remove the header like "data/*;base64,"
        if img.startswith("data:") and ";base64," in img:
            img = img.split(";base64,")[1]
        return img

    def _clean_conf(self, gen_conf):
        options = {}
        if "temperature" in gen_conf:
            options["temperature"] = gen_conf["temperature"]
        if "top_p" in gen_conf:
            options["top_k"] = gen_conf["top_p"]
        if "presence_penalty" in gen_conf:
            options["presence_penalty"] = gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            options["frequency_penalty"] = gen_conf["frequency_penalty"]
        return options

    def _form_history(self, system, history, images=None):
        if images is None:
            images = []
        hist = deepcopy(history)
        if system and hist[0]["role"] == "user":
            hist.insert(0, {"role": "system", "content": system})
        if not images:
            return hist
        temp_images = []
        for img in images:
            temp_images.append(self._clean_img(img))
        for his in hist:
            if his["role"] == "user":
                his["images"] = temp_images
                break
        return hist

    def describe(self, image):
        prompt = self.prompt("")
        try:
            response = self.client.generate(
                model=self.model_name,
                prompt=prompt[0]["content"],
                images=[image],
            )
            ans = response["response"].strip()
            return ans, 128
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def describe_with_prompt(self, image, prompt=None):
        vision_prompt = self.vision_llm_prompt("", prompt) if prompt else self.vision_llm_prompt("")
        try:
            response = self.client.generate(
                model=self.model_name,
                prompt=vision_prompt[0]["content"][0]["text"],
                images=[image],
            )
            ans = response["response"].strip()
            return ans, 128
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat(self, system, history, gen_conf, images=None):
        if images is None:
            images = []
        try:
            response = self.client.chat(model=self.model_name, messages=self._form_history(system, history, images), options=self._clean_conf(gen_conf), keep_alive=self.keep_alive)

            ans = response["message"]["content"].strip()
            return ans, response["eval_count"] + response.get("prompt_eval_count", 0)
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf, images=None):
        if images is None:
            images = []
        ans = ""
        try:
            response = self.client.chat(model=self.model_name, messages=self._form_history(system, history, images), stream=True, options=self._clean_conf(gen_conf), keep_alive=self.keep_alive)
            for resp in response:
                if resp["done"]:
                    yield resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0)
                ans = resp["message"]["content"]
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)
        yield 0
