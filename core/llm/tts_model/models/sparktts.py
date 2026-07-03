import _thread as thread
import base64
import hashlib
import hmac
import json
import queue
import ssl
from datetime import datetime
from time import mktime
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time

import websocket


class SparkTTS:
    STATUS_FIRST_FRAME = 0
    STATUS_CONTINUE_FRAME = 1
    STATUS_LAST_FRAME = 2

    def __init__(self, key, model_name, base_url=""):
        key = json.loads(key)
        self.APPID = key.get("spark_app_id", "xxxxxxx")
        self.APISecret = key.get("spark_api_secret", "xxxxxxx")
        self.APIKey = key.get("spark_api_key", "xxxxxx")
        self.model_name = model_name
        self.CommonArgs = {"app_id": self.APPID}
        self.audio_queue = queue.Queue()

    # 用来存储音频数据

    # 生成url
    def create_url(self):
        url = 'wss://tts-api.xfyun.cn/v2/tts'
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/tts " + "HTTP/1.1"
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                                 digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        url = url + '?' + urlencode(v)
        return url

    def tts(self, text):
        BusinessArgs = {"aue": "lame", "sfl": 1, "auf": "audio/L16;rate=16000", "vcn": self.model_name, "tte": "utf8"}
        Data = {"status": 2, "text": base64.b64encode(text.encode('utf-8')).decode('utf-8')}
        CommonArgs = {"app_id": self.APPID}
        audio_queue = self.audio_queue
        model_name = self.model_name

        class Callback:
            def __init__(self):
                self.audio_queue = audio_queue

            def on_message(self, ws, message):
                message = json.loads(message)
                code = message["code"]
                sid = message["sid"]
                audio = message["data"]["audio"]
                audio = base64.b64decode(audio)
                status = message["data"]["status"]
                if status == 2:
                    ws.close()
                if code != 0:
                    errMsg = message["message"]
                    raise Exception(f"sid:{sid} call error:{errMsg} code:{code}")
                else:
                    self.audio_queue.put(audio)

            def on_error(self, ws, error):
                raise Exception(error)

            def on_close(self, ws, close_status_code, close_msg):
                self.audio_queue.put(None)  # None is terminator

            def on_open(self, ws):
                def run(*args):
                    d = {"common": CommonArgs,
                         "business": BusinessArgs,
                         "data": Data}
                    ws.send(json.dumps(d))

                thread.start_new_thread(run, ())

        wsUrl = self.create_url()
        websocket.enableTrace(False)
        a = Callback()
        ws = websocket.WebSocketApp(wsUrl, on_open=a.on_open, on_error=a.on_error, on_close=a.on_close,
                                    on_message=a.on_message)
        status_code = 0
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        while True:
            audio_chunk = self.audio_queue.get()
            if audio_chunk is None:
                if status_code == 0:
                    raise Exception(
                        f"Fail to access model({model_name}) using the provided credentials. **ERROR**: Invalid APPID, API Secret, or API Key.")
                else:
                    break
            status_code = 1
            yield audio_chunk
