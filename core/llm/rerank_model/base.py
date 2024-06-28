# rerank_model/base.py
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List

@dataclass
class Base(ABC):
    key: str
    model_name: str

    @abstractmethod
    def similarity(self, query: str, texts: List[str]) -> List[float]:
        raise NotImplementedError("Please implement similarity method!")

