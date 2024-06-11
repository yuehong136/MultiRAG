# embedding_model/base.py
from abc import ABC, abstractmethod

class Base(ABC):
    def __init__(self, key, model_name):
        self.key = key
        self.model_name = model_name

    @abstractmethod
    def encode(self, texts: list, batch_size=32):
        raise NotImplementedError("Please implement encode method!")

    @abstractmethod
    def encode_queries(self, text: str):
        raise NotImplementedError("Please implement encode method!")
