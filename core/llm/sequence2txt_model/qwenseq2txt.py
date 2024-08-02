# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
from core.llm.sequence2txt_model.base import Base
from core.utils import num_tokens_from_string


class QWenSeq2txt(Base):
    def __init__(self, key, model_name="paraformer-realtime-8k-v1", **kwargs):
        import dashscope
        dashscope.api_key = key
        self.model_name = model_name

    def transcription(self, audio, format):
        from http import HTTPStatus
        from dashscope.audio.asr import Recognition

        recognition = Recognition(model=self.model_name,
                                  format=format,
                                  sample_rate=16000,
                                  callback=None)
        result = recognition.call(audio)

        ans = ""
        if result.status_code == HTTPStatus.OK:
            for sentence in result.get_sentence():
                ans += str(sentence + '\n')
            return ans, num_tokens_from_string(ans)

        return "**ERROR**: " + result.message, 0