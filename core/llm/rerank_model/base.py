# rerank_model/base.py
from abc import ABC, abstractmethod

class Base(ABC):
    def __init__(self, key, model_name):
        self.key = key
        self.model_name = model_name

    @abstractmethod
    def similarity(self, query: str, texts: list):
        raise NotImplementedError("Please implement similarity method!")
