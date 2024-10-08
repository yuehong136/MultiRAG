import requests


class XinferenceTTS:
    def __init__(self, key, model_name, **kwargs):
        self.base_url = kwargs.get("base_url", None)
        self.model_name = model_name
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    def tts(self, text, voice="中文女", stream=True):
        payload = {
            "model": self.model_name,
            "input": text,
            "voice": voice
        }

        response = requests.post(
            f"{self.base_url}/v1/audio/speech",
            headers=self.headers,
            json=payload,
            stream=stream
        )

        if response.status_code != 200:
            raise Exception(f"**Error**: {response.status_code}, {response.text}")

        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                yield chunk
