# coding=utf-8
"""
@project: multirag
@Author：龙
@file： zhipu_embed.py
@date：2024/8/8 9:00
@desc:
"""
import numpy as np
from dataclasses import dataclass, field
from zhipuai import ZhipuAI
from core.llm.embedding_model.base import Base


@dataclass
class ZhipuEmbed(Base):
    key: str = None
    model_name: str = "embedding-2"
    base_url: str | None = None
    client: ZhipuAI = field(init=False)

    def __post_init__(self):
        self.client = ZhipuAI(api_key=self.key)
        print(f"ZhipuAI client initialized with key: {self.key}")

    def encode(self, texts: list, batch_size=16):
        arr = []
        tks_num = 0
        for txt in texts:
            res = self.client.embeddings.create(input=txt,
                                                model=self.model_name, dimensions=768)
            arr.append(res.data[0].embedding)
            tks_num += res.usage.total_tokens
        return np.array(arr), tks_num

    def encode_queries(self, text):
        res = self.client.embeddings.create(input=text,
                                            model=self.model_name)
        return np.array(res.data[0].embedding), res.usage.total_tokens