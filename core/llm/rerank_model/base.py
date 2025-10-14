from abc import ABC
import numpy as np


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
        if hasattr(resp, "usage") and hasattr(resp.usage, "total_tokens"):
            try:
                return resp.usage.total_tokens
            except Exception:
                pass

        if 'usage' in resp and 'total_tokens' in resp['usage']:
            try:
                return resp["usage"]["total_tokens"]
            except Exception:
                pass
        return 0

