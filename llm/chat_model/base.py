# base.py
from abc import ABC, abstractmethod

class Base(ABC):
    def __init__(self, key, model_name, base_url=None):
        self.key = key
        self.model_name = model_name
        self.base_url = base_url

    @abstractmethod
    def chat(self, system, history, gen_conf):
        pass

    @abstractmethod
    def chat_streamly(self, system, history, gen_conf):
        pass
