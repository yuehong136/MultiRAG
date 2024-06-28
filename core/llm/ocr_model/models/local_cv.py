from dataclasses import dataclass
from typing import Union, Tuple
from core.llm.ocr_model.base import Base
from PIL import Image
from io import BytesIO

@dataclass
class LocalCV(Base):
    key: str
    model_name: str = "glm-4v"
    lang: str = "Chinese"

    def describe(self, image: Union[bytes, BytesIO, Image.Image], max_tokens: int = 1024) -> Tuple[str, int]:
        # 返回一个空字符串和零 tokens
        return "", 0
