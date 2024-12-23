from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Base(ABC):
    key: str
    model_name: str

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Please implement encode method!")

    @abstractmethod
    def encode_queries(self, text: str) -> list[float]:
        raise NotImplementedError("Please implement encode method!")
