import tempfile
from pathlib import Path

from common.token_utils import num_tokens_from_string
from core.llm.cv_model.models.gptv4 import GptV4


class QWenCV(GptV4):
    _FACTORY_NAME = "Tongyi-Qianwen"

    def __init__(self, key, model_name="qwen-vl-chat-v1", lang="Chinese", base_url=None, **kwargs):
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        super().__init__(key, model_name, lang=lang, base_url=base_url, **kwargs)

    def chat(self, system, history, gen_conf, images=None, video_bytes=None, filename="", **kwargs):
        if video_bytes:
            try:
                summary, summary_num_tokens = self._process_video(video_bytes, filename)
                return summary, summary_num_tokens
            except Exception as e:
                return "**ERROR**: " + str(e), 0

        return "**ERROR**: Method chat not supported yet.", 0

    def _process_video(self, video_bytes, filename):
        from dashscope import MultiModalConversation

        video_suffix = Path(filename).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=video_suffix) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

            video_path = f"file://{tmp_path}"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "video": video_path,
                            "fps": 2,
                        },
                        {
                            "text": "Please summarize this video in proper sentences.",
                        },
                    ],
                }
            ]

            def call_api():
                response = MultiModalConversation.call(
                    api_key=self.api_key,
                    model=self.model_name,
                    messages=messages,
                )
                if response.get("message"):
                    raise Exception(response["message"])
                summary = response["output"]["choices"][0]["message"].content[0]["text"]
                return summary, num_tokens_from_string(summary)

            try:
                return call_api()
            except Exception as e1:
                import dashscope

                dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
                try:
                    return call_api()
                except Exception as e2:
                    raise RuntimeError(f"Both default and intl endpoint failed.\nFirst error: {e1}\nSecond error: {e2}")
