import numpy as np
import requests

from core.llm.embedding_model.base import Base
from core.utils import num_tokens_from_string


class HuggingFaceEmbed(Base):
    def __init__(self, key, model_name, base_url=None):
        if not model_name:
            raise ValueError("Model name cannot be None")
        self.key = key
        self.model_name = model_name.split("___")[0]
        self.base_url = base_url or "http://127.0.0.1:8080"

    def encode(self, texts: list):
        embeddings = []
        for text in texts:
            response = requests.post(
                f"{self.base_url}/embed",
                json={"inputs": text},
                headers={'Content-Type': 'application/json'}
            )
            if response.status_code == 200:
                embedding = response.json()
                embeddings.append(embedding[0])
            else:
                raise Exception(f"Error: {response.status_code} - {response.text}")
        return np.array(embeddings), sum([num_tokens_from_string(text) for text in texts])

    def encode_queries(self, text):
        response = requests.post(
            f"{self.base_url}/embed",
            json={"inputs": text},
            headers={'Content-Type': 'application/json'}
        )
        if response.status_code == 200:
            embedding = response.json()
            return np.array(embedding[0]), num_tokens_from_string(text)
        else:
            raise Exception(f"Error: {response.status_code} - {response.text}")

