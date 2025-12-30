from abc import ABC
import numpy as np
from common.token_utils import total_token_count_from_response

def sigmoid(x):
    return 1 / (1 + np.exp(-x))


class Base(ABC):
    def __init__(self, key, model_name, **kwargs):
        """
        Abstract base class constructor.
        Parameters are not stored; initialization is left to subclasses.
        """
        pass

    def similarity(self, query: str, texts: list):
        raise NotImplementedError("Please implement encode method!")

    def total_token_count(self, resp):
        return total_token_count_from_response(resp)

