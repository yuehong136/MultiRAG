from abc import ABC

class Base(ABC):
    def __init__(self, key, model_name, base_url):
        pass

    def transcription(self, audio):
        pass
