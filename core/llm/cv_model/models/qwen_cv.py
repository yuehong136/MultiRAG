import io
import os
from PIL import Image

from api.utils import get_uuid
from api.utils.file_utils import get_project_base_directory
from core.llm.cv_model.base import Base
from core.nlp import is_english
from core.prompts import vision_llm_describe_prompt


class QWenCV(Base):
    def __init__(self, key, model_name="qwen-vl-chat-v1", lang="Chinese", **kwargs):
        import dashscope
        dashscope.api_key = key
        self.model_name = model_name
        self.lang = lang

    def prompt(self, binary):
        # stupid as hell
        tmp_dir = get_project_base_directory("tmp")
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, "%s.jpg" % get_uuid())
        # Image.open(io.BytesIO(binary)).save(path)

        img = Image.open(io.BytesIO(binary))
        # Convert RGBA to RGB before saving as JPEG
        if img.mode == 'RGBA':
            img = img.convert('RGB')

        img.save(path)

        return [
            {
                "role": "user",
                "content": [
                    {
                        "image": f"file://{path}"
                    },
                    {
                        "text": "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。" if self.lang.lower() == "chinese" else
                        "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out.",
                    },
                ],
            }
        ]

    def vision_llm_prompt(self, binary, prompt=None):
        # stupid as hell
        tmp_dir = get_project_base_directory("tmp")
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, "%s.jpg" % get_uuid())
        Image.open(io.BytesIO(binary)).save(path)

        return [
            {
                "role": "user",
                "content": [
                    {
                        "image": f"file://{path}"
                    },
                    {
                        "text":  prompt if prompt else vision_llm_describe_prompt(),
                    },
                ],
            }
        ]

    def chat_prompt(self, text, b64):
        return [
            {"image": f"{b64}"},
            {"text": text},
        ]

    def describe(self, image):
        from http import HTTPStatus

        from dashscope import MultiModalConversation
        response = MultiModalConversation.call(model=self.model_name, messages=self.prompt(image))
        if response.status_code == HTTPStatus.OK:
            return response.output.choices[0]['message']['content'][0]["text"], response.usage.output_tokens
        return response.message, 0

    def describe_with_prompt(self, image, prompt=None):
        from http import HTTPStatus

        from dashscope import MultiModalConversation

        vision_prompt = self.vision_llm_prompt(image, prompt) if prompt else self.vision_llm_prompt(image)
        response = MultiModalConversation.call(model=self.model_name, messages=vision_prompt)
        if response.status_code == HTTPStatus.OK:
            return response.output.choices[0]['message']['content'][0]["text"], response.usage.output_tokens
        return response.message, 0

    def chat(self, system, history, gen_conf, image=""):
        from http import HTTPStatus

        from dashscope import MultiModalConversation
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                if image:
                    his["content"] = self.chat_prompt(his["content"], image)
                else:
                    his["content"] = self.chat_onlytext(his["content"])
        response = MultiModalConversation.call(model=self.model_name, messages=history,
                                               temperature=gen_conf.get("temperature", 0.3),
                                               top_p=gen_conf.get("top_p", 0.7))

        ans = ""
        tk_count = 0
        if response.status_code == HTTPStatus.OK:
            ans = response.output.choices[0]['message']['content'][0]["text"]
            tk_count += response.usage.total_tokens
            if response.output.choices[0].get("finish_reason", "") == "length":
                ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                    [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
            return ans, tk_count

        return "**ERROR**: " + response.message, tk_count

    def chat_streamly(self, system, history, gen_conf, image=""):
        from http import HTTPStatus

        from dashscope import MultiModalConversation
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                if image:
                    his["content"] = self.chat_prompt(his["content"], image)
                else:
                    his["content"] = self.chat_onlytext(his["content"])

        ans = ""
        tk_count = 0
        try:
            response = MultiModalConversation.call(model=self.model_name, messages=history,
                                                   temperature=gen_conf.get("temperature", 0.3),
                                                   top_p=gen_conf.get("top_p", 0.7),
                                                   stream=True)
            for resp in response:
                if resp.status_code == HTTPStatus.OK:
                    ans = resp.output.choices[0]['message']['content'][0]["text"]
                    tk_count = resp.usage.total_tokens
                    if resp.output.choices[0].get("finish_reason", "") == "length":
                        ans += "...\nFor the content length reason, it stopped, continue?" if is_english(
                            [ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                    yield ans
                else:
                    yield ans + "\n**ERROR**: " + resp.message if str(resp.message).find(
                        "Access") < 0 else "Out of credit. Please set the API key in **settings > Model providers.**"
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count