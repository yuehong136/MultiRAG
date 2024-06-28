from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

@dataclass
class Base(ABC):
    key: str
    model_name: str

    @abstractmethod
    def encode(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        raise NotImplementedError("Please implement encode method!")

    @abstractmethod
    def encode_queries(self, text: str) -> List[float]:
        raise NotImplementedError("Please implement encode method!")
