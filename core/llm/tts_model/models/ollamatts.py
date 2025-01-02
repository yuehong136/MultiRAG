import requests

from core.llm.tts_model.base import Base


class OllamaTTS(Base):
    def __init__(self, key, model_name="ollama-tts", base_url="https://api.ollama.ai/v1"):
        if not base_url:
            base_url = "https://api.ollama.ai/v1"
        self.model_name = model_name
        self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json"
        }

    def tts(self, text, voice="standard-voice"):
        payload = {
            "model": self.model_name,
            "voice": voice,
            "input": text
        }

        response = requests.post(f"{self.base_url}/audio/tts", headers=self.headers, json=payload, stream=True)

        if response.status_code != 200:
            raise Exception(f"**Error**: {response.status_code}, {response.text}")

        for chunk in response.iter_content():
            if chunk:
                yield chunk