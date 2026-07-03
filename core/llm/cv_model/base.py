"""
@project: multirag
@Author：龙
@file： core.llm.cv_model.base.py
@date：2024/7/22 9:30
@desc:
"""

import base64
import logging
import os
from abc import ABC
from io import BytesIO
from typing import Any

from core.nlp import is_english
from core.prompts.generator import vision_llm_describe_prompt


class Base(ABC):
    def __init__(self, **kwargs):
        # Configure retry parameters
        self.max_retries = kwargs.get("max_retries", int(os.environ.get("LLM_MAX_RETRIES", 5)))
        self.base_delay = kwargs.get("retry_interval", float(os.environ.get("LLM_BASE_DELAY", 2.0)))
        self.max_rounds = kwargs.get("max_rounds", 5)
        self.is_tools = False
        self.tools = []
        self.toolcall_sessions = {}

    def describe(self, image):
        raise NotImplementedError("Please implement encode method!")

    def describe_with_prompt(self, image, prompt=None):
        raise NotImplementedError("Please implement encode method!")

    def _form_history(self, system, history, images=None):
        if images is None:
            images = []
        hist = []
        if system:
            hist.append({"role": "system", "content": system})
        for h in history:
            if images and h["role"] == "user":
                h["content"] = self._image_prompt(h["content"], images)
                images = []
            hist.append(h)
        return hist

    def _image_prompt(self, text, images):
        if not images:
            return text

        if isinstance(images, str) or "bytes" in type(images).__name__:
            images = [images]

        pmpt = [{"type": "text", "text": text}]
        for img in images:
            try:
                pmpt.append({"type": "image_url", "image_url": {"url": self._normalize_image(img)}})
            except Exception:
                logging.warning("[%s] Skip invalid image input in request payload.", self.__class__.__name__)
                continue
        return pmpt

    def chat(self, system, history, gen_conf, images=None, **kwargs):
        if images is None:
            images = []
        try:
            response = self.client.chat.completions.create(model=self.model_name, messages=self._form_history(system, history, images))
            return response.choices[0].message.content.strip(), response.usage.total_tokens
        except Exception as e:
            return "**ERROR**: " + str(e), 0

    def chat_streamly(self, system, history, gen_conf, images=None, **kwargs):
        if images is None:
            images = []
        ans = ""
        tk_count = 0
        try:
            response = self.client.chat.completions.create(model=self.model_name, messages=self._form_history(system, history, images), stream=True)
            for resp in response:
                if not resp.choices[0].delta.content:
                    continue
                delta = resp.choices[0].delta.content
                ans = delta
                if resp.choices[0].finish_reason == "length":
                    ans += "...\nFor the content length reason, it stopped, continue?" if is_english([ans]) else "······\n由于长度的原因，回答被截断了，要继续吗？"
                if resp.choices[0].finish_reason == "stop":
                    tk_count += resp.usage.total_tokens
                yield ans
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count

    @staticmethod
    def image2base64(image):
        # Return a data URL with the correct MIME to avoid provider mismatches
        if isinstance(image, bytes):
            # Best-effort magic number sniffing
            mime = "image/png"
            if len(image) >= 2 and image[0] == 0xFF and image[1] == 0xD8:
                mime = "image/jpeg"
            b64 = base64.b64encode(image).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        if isinstance(image, BytesIO):
            data = image.getvalue()
            mime = "image/png"
            if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
                mime = "image/jpeg"
            b64 = base64.b64encode(data).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        with BytesIO() as buffered:
            fmt = "JPEG"
            try:
                image.save(buffered, format="JPEG")
            except Exception:
                # reset buffer before saving PNG
                buffered.seek(0)
                buffered.truncate()
                image.save(buffered, format="PNG")
                fmt = "PNG"
            data = buffered.getvalue()
            b64 = base64.b64encode(data).decode("utf-8")
            mime = f"image/{fmt.lower()}"
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _blob_to_data_url(blob, mime_type="image/png"):
        if isinstance(blob, str):
            blob = blob.strip()
            if blob.startswith("data:") or blob.startswith("http://") or blob.startswith("https://") or blob.startswith("file://"):
                return blob
            return f"data:{mime_type};base64,{blob}"
        if isinstance(blob, BytesIO):
            blob = blob.getvalue()
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if isinstance(blob, bytearray):
            blob = bytes(blob)
        if isinstance(blob, bytes):
            b64 = base64.b64encode(blob).decode("utf-8")
            return f"data:{mime_type};base64,{b64}"
        return None

    def _normalize_image(self, image):
        if isinstance(image, dict):
            inline_data = image.get("inline_data")
            if isinstance(inline_data, dict):
                mime = inline_data.get("mime_type") or "image/png"
                data_url = self._blob_to_data_url(inline_data.get("data"), mime)
                if data_url:
                    return data_url

            image_url = image.get("image_url")
            if isinstance(image_url, dict):
                data_url = self._blob_to_data_url(image_url.get("url"), image.get("mime_type") or "image/png")
                if data_url:
                    return data_url
            if isinstance(image_url, str):
                data_url = self._blob_to_data_url(image_url, image.get("mime_type") or "image/png")
                if data_url:
                    return data_url

            if "url" in image:
                data_url = self._blob_to_data_url(image.get("url"), image.get("mime_type") or "image/png")
                if data_url:
                    return data_url

            mime = image.get("mime_type") or image.get("media_type") or "image/png"
            for key in ("blob", "data"):
                if key in image:
                    data_url = self._blob_to_data_url(image.get(key), mime)
                    if data_url:
                        return data_url

        if isinstance(image, (bytes, bytearray, memoryview, BytesIO)):
            return self.image2base64(image)
        if isinstance(image, str):
            return self._blob_to_data_url(image, "image/png")
        return self.image2base64(image)

    def prompt(self, b64):
        return [
            {
                "role": "user",
                "content": self._image_prompt(
                    "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。"
                    if self.lang.lower() == "chinese"
                    else "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out.",
                    b64,
                ),
            }
        ]

    def vision_llm_prompt(self, b64, prompt=None):
        return [{"role": "user", "content": self._image_prompt(prompt if prompt else vision_llm_describe_prompt(), b64)}]

    def chat_prompt(self, text: str, b64: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "image_url",
                "image_url": {
                    # "url": f"data:image/jpeg;base64,{b64}",
                    "url": f"{b64}",
                },
            },
            {"type": "text", "text": text},
        ]

    def chat_onlytext(self, text: str) -> list[dict[str, Any]]:
        return [
            {"type": "text", "text": text},
        ]
