import requests

from core.llm.tts_model.base import Base


class SILICONFLOWTTS(Base):
    def __init__(self, key, model_name="FunAudioLLM/CosyVoice2-0.5B", base_url="https://api.siliconflow.cn/v1"):
        if not base_url:
            base_url = "https://api.siliconflow.cn/v1"
        self.api_key = key
        self.model_name = model_name
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def tts(self, text, voice="anna", sample_rate=44100):
        text = self.normalize_text(text)
        # 校验，只接受 32000 或 44100
        if sample_rate not in (32000, 44100):
            raise ValueError("sample_rate must be 32000 or 44100")

        payload = {"model": self.model_name, "input": text, "voice": f"{self.model_name}:{voice}", "response_format": "mp3", "sample_rate": sample_rate, "stream": True, "speed": 1, "gain": 0}

        response = requests.post(f"{self.base_url}/audio/speech", headers=self.headers, json=payload)
        if response.status_code != 200:
            raise Exception(f"**Error**: {response.status_code}, {response.text}")
        for chunk in response.iter_content():
            if chunk:
                yield chunk
