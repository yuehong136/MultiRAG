import requests

from core.llm.tts_model.base import Base


class OpenAITTS(Base):
    def __init__(self, key, model_name="tts-1", base_url="https://api.openai.com/v1"):
        if not base_url:
            base_url="https://api.openai.com/v1"
        self.api_key = key
        self.model_name = model_name
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def tts(self, text, voice="alloy"):
        text = self.normalize_text(text)
        payload = {
            "model": self.model_name,
            "voice": voice,
            "input": text
        }

        response = requests.post(f"{self.base_url}/audio/speech", headers=self.headers, json=payload, stream=True)

        if response.status_code != 200:
            raise Exception(f"**Error**: {response.status_code}, {response.text}")
        for chunk in response.iter_content():
            if chunk:
                yield chunk
