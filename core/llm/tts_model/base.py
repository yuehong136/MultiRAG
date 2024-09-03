import re
from abc import ABC


class Base(ABC):
    def __init__(self, key, model_name, base_url):
        pass

    def tts(self, audio):
        pass

    def normalize_text(self, text):
        return re.sub(r'(\*\*|##\d+\$\$|#)', '', text)
