from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Union, List, Dict, Any
import base64
from io import BytesIO
from PIL import Image

@dataclass
class Base(ABC):
    key: str
    model_name: str

    @abstractmethod
    def describe(self, image: Union[bytes, BytesIO, Image.Image], max_tokens: int = 300) -> str:
        raise NotImplementedError("Please implement the describe method!")

    def image2base64(self, image: Union[bytes, BytesIO, Image.Image]) -> str:
        if isinstance(image, bytes):
            return base64.b64encode(image).decode("utf-8")
        if isinstance(image, BytesIO):
            return base64.b64encode(image.getvalue()).decode("utf-8")
        if isinstance(image, Image.Image):
            buffered = BytesIO()
            try:
                image.save(buffered, format="JPEG")
            except Exception:
                image.save(buffered, format="PNG")
            return base64.b64encode(buffered.getvalue()).decode("utf-8")
        raise TypeError("Unsupported image type")

    def prompt(self, b64: str, lang: str = "Chinese") -> List[Dict[str, Any]]:
        content = "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。" if lang.lower() == "chinese" else \
                  "Please describe the content of this picture, like where, when, who, what happened. If it has number data, please extract them out."
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        },
                    },
                    {
                        "text": content
                    },
                ],
            }
        ]
