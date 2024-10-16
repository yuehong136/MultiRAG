# coding=utf-8
"""
@project: multirag
@Author：龙
@file： qwenseq2txt.py
@date：2024/10/08 11:05
@desc:
"""
from core.llm.sequence2txt_model.base import Base
from core.utils import num_tokens_from_string

class QWenSeq2txt(Base):
    def __init__(self, key, model_name="paraformer-realtime-8k-v1", **kwargs):
        import dashscope
        dashscope.api_key = key
        self.model_name = model_name

    def transcription(self, audio, format="mp3"):
        from http import HTTPStatus
        from dashscope.audio.asr import Recognition

        recognition = Recognition(model=self.model_name,
                                  format=format,
                                  sample_rate=16000,
                                  callback=None)
        result = recognition.call(audio)
        print(result)
        ans = ""
        if result.status_code == HTTPStatus.OK:
            for sentence in result.get_sentence():
                # 使用字典的 'text' 键来获取转录内容
                ans += sentence['text'] + '\n'
            return ans, num_tokens_from_string(ans)

        return "**ERROR**: " + result.message, 0

if __name__ == "__main__":
    # 设置API密钥和音频文件URL
    api_key = "sk-4b492b0a99004a7da958b669858a2bdd"
    audio_url = "test1.wav"  # 将此替换为有效的音频文件URL

    # 初始化QWenSeq2txt类
    model = QWenSeq2txt(api_key)

    # 异步调用转写方法并获取结果
    transcription, token_count = model.transcription(audio=audio_url)

    # 输出转录结果
    print("转录结果：")
    print(transcription)
    print("令牌数量：", token_count)