import json
import re

from common.token_utils import num_tokens_from_string
from core.llm.sequence2txt_model.base import Base


class TencentCloudSeq2txt(Base):
    def __init__(
            self, key, model_name="16k_zh", base_url="https://asr.tencentcloudapi.com"
    ):
        from tencentcloud.asr.v20190614 import asr_client
        from tencentcloud.common import credential

        key = json.loads(key)
        sid = key.get("tencent_cloud_sid", "")
        sk = key.get("tencent_cloud_sk", "")
        cred = credential.Credential(sid, sk)
        self.client = asr_client.AsrClient(cred, "")
        self.model_name = model_name

    def transcription(self, audio, max_retries=60, retry_interval=5):
        import time

        from tencentcloud.asr.v20190614 import models
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )

        b64 = self.audio2base64(audio)
        try:
            # dispatch disk
            req = models.CreateRecTaskRequest()
            params = {
                "EngineModelType": self.model_name,
                "ChannelNum": 1,
                "ResTextFormat": 0,
                "SourceType": 1,
                "Data": b64,
            }
            req.from_json_string(json.dumps(params))
            resp = self.client.CreateRecTask(req)

            # loop query
            req = models.DescribeTaskStatusRequest()
            params = {"TaskId": resp.Data.TaskId}
            req.from_json_string(json.dumps(params))
            retries = 0
            while retries < max_retries:
                resp = self.client.DescribeTaskStatus(req)
                if resp.Data.StatusStr == "success":
                    text = re.sub(
                        r"\[\d+:\d+\.\d+,\d+:\d+\.\d+\]\s*", "", resp.Data.Result
                    ).strip()
                    return text, num_tokens_from_string(text)
                elif resp.Data.StatusStr == "failed":
                    return (
                        "**ERROR**: Failed to retrieve speech recognition results.",
                        0,
                    )
                else:
                    time.sleep(retry_interval)
                    retries += 1
            return "**ERROR**: Max retries exceeded. Task may still be processing.", 0

        except TencentCloudSDKException as e:
            return "**ERROR**: " + str(e), 0
        except Exception as e:
            return "**ERROR**: " + str(e), 0
