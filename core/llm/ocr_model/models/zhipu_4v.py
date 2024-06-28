from dataclasses import dataclass, field
from io import BytesIO
from typing import Union, Tuple, Any, List, Dict
from core.llm.ocr_model.base import Base
from zhipuai import ZhipuAI
from PIL import Image

@dataclass
class Zhipu4V(Base):
    key: str
    model_name: str = "glm-4v"
    lang: str = "Chinese"
    client: ZhipuAI = field(init=False)

    def __post_init__(self):
        self.client = ZhipuAI(api_key=self.key)

    def describe(self, image: Union[bytes, BytesIO, Image.Image], max_tokens: int = 1024) -> Tuple[str, int]:
        b64 = self.image2base64(image)
        # try:
        res = self.client.chat.completions.create(
            model=self.model_name,
            messages=self.prompt(b64, self.lang),
            max_tokens=max_tokens,
        )
        return res.choices[0].message.content.strip(), res.usage.total_tokens
        # except Exception as e:
        #     return f"**ERROR**: {str(e)}", 0

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
                        "type": "text",
                        "text": content
                    },
                ],
            }
        ]
