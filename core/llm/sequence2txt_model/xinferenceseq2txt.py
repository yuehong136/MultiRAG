"""
@project: multirag
@Author：龙
@file： xinferenceseq2txt.py
@date：2024/8/2 9:00
@desc:
"""

import requests

from common.token_utils import num_tokens_from_string
from core.llm.sequence2txt_model.base import Base


class XinferenceSeq2txt(Base):
    def __init__(self, key, model_name="whisper-small", **kwargs):
        self.base_url = kwargs.get("base_url", None)
        self.model_name = model_name
        self.key = key

    def transcription(self, audio, language="zh", prompt=None, response_format="json", temperature=0.7):
        if isinstance(audio, str):
            audio_file = open(audio, "rb")
            audio_data = audio_file.read()
            audio_file_name = audio.split("/")[-1]
        else:
            audio_data = audio
            audio_file_name = "audio.wav"

        payload = {"model": self.model_name, "language": language, "prompt": prompt, "response_format": response_format, "temperature": temperature}

        files = {"file": (audio_file_name, audio_data, "audio/wav")}

        try:
            response = requests.post(f"{self.base_url}/v1/audio/transcriptions", files=files, data=payload)
            response.raise_for_status()
            result = response.json()

            if "text" in result:
                transcription_text = result["text"].strip()
                return transcription_text, num_tokens_from_string(transcription_text)
            else:
                return "**ERROR**: Failed to retrieve transcription.", 0

        except requests.exceptions.RequestException as e:
            return f"**ERROR**: {e!s}", 0
