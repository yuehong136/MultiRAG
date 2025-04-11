import requests


class GPUStackTTS:
    def __init__(self, key, model_name, **kwargs):
        self.base_url = kwargs.get("base_url", None)
        self.api_key = key
        self.model_name = model_name
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    def tts(self, text, voice="Chinese Female", stream=True):
        payload = {
            "model": self.model_name,
            "input": text,
            "voice": voice
        }

        response = requests.post(
            f"{self.base_url}/v1-openai/audio/speech",
            headers=self.headers,
            json=payload,
            stream=stream
        )

        if response.status_code != 200:
            raise Exception(f"**Error**: {response.status_code}, {response.text}")

        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                yield chunk