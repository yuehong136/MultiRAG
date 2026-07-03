"""
@project: multirag
@Author：龙
@file： qwenseq2txt.py
@date：2024/10/08 11:05
@desc:
"""

from common.token_utils import num_tokens_from_string
from core.llm.sequence2txt_model.base import Base


class QWenSeq2txt(Base):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="qwen3-asr-flash", **kwargs):
        import dashscope

        dashscope.api_key = key
        self.model_name = model_name

    def transcription(self, audio_path):
        if "paraformer" in self.model_name or "sensevoice" in self.model_name:
            return f"**ERROR**: model {self.model_name} is not suppported yet.", 0

        from dashscope import MultiModalConversation

        audio_path = f"file://{audio_path}"
        messages = [
            {
                "role": "system",
                "content": [
                    {"text": ""},  # 用于上下文增强
                ],
            },
            {
                "role": "user",
                "content": [{"audio": audio_path}],
            },
        ]

        response = None
        full_content = ""
        try:
            response_stream = MultiModalConversation.call(
                model=self.model_name,
                messages=messages,
                result_format="message",
                stream=True,
                asr_options={
                    "enable_lid": True,  # 启用语种识别
                    "enable_itn": False,  # 逆文本规范化
                },
            )
            # 流式响应已自动累加，获取最后一个响应即可
            last_response = None
            for response in response_stream:
                last_response = response

            # 从最后一个响应提取完整内容
            if last_response:
                try:
                    full_content = last_response["output"]["choices"][0]["message"].content[0]["text"]
                except Exception:
                    pass

            return full_content, num_tokens_from_string(full_content)
        except Exception as e:
            return "**ERROR**: " + str(e), 0


# class QWenSeq2txt(Base):
#     def __init__(self, key, model_name="paraformer-realtime-8k-v1", **kwargs):
#         import dashscope
#         dashscope.api_key = key
#         self.model_name = model_name
#
#     def transcription(self, audio, format="mp3"):
#         from http import HTTPStatus
#         from dashscope.audio.asr import Recognition
#
#         recognition = Recognition(model=self.model_name,
#                                   format=format,
#                                   sample_rate=16000,
#                                   callback=None)
#         result = recognition.call(audio)
#         print(result)
#         ans = ""
#         if result.status_code == HTTPStatus.OK:
#             for sentence in result.get_sentence():
#                 # 使用字典的 'text' 键来获取转录内容
#                 ans += sentence['text'] + '\n'
#             return ans, num_tokens_from_string(ans)
#
#         return "**ERROR**: " + result.message, 0
