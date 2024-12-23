import numpy as np
from openai import OpenAI

from core.llm.embedding_model.base import Base
from core.utils import truncate


class OpenAIEmbed(Base):
    def __init__(self, key, model_name="text-embedding-ada-002",
                 base_url="https://api.openai.com/v1"):
        if not base_url:
            base_url = "https://api.openai.com/v1"
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model_name = model_name

    def encode(self, texts: list):
        # OpenAI requires batch size <=16
        batch_size = 16
        texts = [truncate(t, 8191) for t in texts]
        ress = []
        total_tokens = 0
        for i in range(0, len(texts), batch_size):
            res = self.client.embeddings.create(input=texts[i:i + batch_size],
                                                model=self.model_name)
            ress.extend([d.embedding for d in res.data])
            total_tokens += res.usage.total_tokens
        return np.array(ress), total_tokens

    def encode_queries(self, text):
        res = self.client.embeddings.create(input=[truncate(text, 8191)],
                                            model=self.model_name)
        return np.array(res.data[0].embedding), res.usage.total_tokens