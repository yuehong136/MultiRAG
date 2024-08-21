from dataclasses import dataclass
from abc import ABC, abstractmethod
import numpy as np


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


@dataclass
class Base(ABC):
    key: str
    model_name: str

    def __init__(self, key, model_name):
        self.key = key
        self.model_name = model_name

    @abstractmethod
    def similarity(self, query: str, texts: list):
        raise NotImplementedError("Please implement encode method!")

