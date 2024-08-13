# coding=utf-8
"""
@project: multirag
@Author：龙
@file： sequence2txt_model.base.py
@date：2024/8/12 9:00
@desc:
"""
from abc import ABC
from core.utils import num_tokens_from_string


class Base(ABC):
    def __init__(self, key, model_name):
        pass

    def transcription(self, audio, **kwargs):
        transcription = self.client.audio.transcriptions.create(
            model=self.model_name,
            file=audio,
            response_format="text"
        )
        return transcription.text.strip(), num_tokens_from_string(transcription.text.strip())
