import os

from core.llm.sequence2txt_model.base import Base


class GPUStackSeq2txt(Base):
    def __init__(self, key, model_name, base_url):
        if not base_url:
            raise ValueError("url cannot be None")
        if base_url.split("/")[-1] != "v1-openai":
            base_url = os.path.join(base_url, "v1-openai")
        self.base_url = base_url
        self.model_name = model_name
        self.key = key
