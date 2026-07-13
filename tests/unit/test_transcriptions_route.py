"""chat_api.transcriptions 路由契约（restful_apis AsyncSession 收口，目录清零提交）。

非流式：配置查询与 LLMBundle 构造走 run_sync、facade 剥离（变异验证锚点）、
ASR 同步 HTTP 在工作线程执行；流式：同步 ASR 流经线程池逐项拉取，SSE 帧钉板。
"""

import sys
import threading

import pytest

from api.db.services.llm_service import LLMBundle


def _route_module():
    return sys.modules["api.apps.restful_apis.chat"]


class _FakeASRBundle(LLMBundle):
    """继承真类过 beartype；记录 db 剥离状态与执行线程。"""

    instances: list["_FakeASRBundle"] = []

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        self.seen: dict[str, object] = {}
        type(self).instances.append(self)

    def transcription(self, audio):
        self.seen["db_at_use"] = self.db
        self.seen["off_loop"] = threading.current_thread() is not threading.main_thread()
        return "hello world"

    def stream_transcription(self, audio):
        for i in range(2):
            self.seen.setdefault("stream_threads", []).append(threading.current_thread() is not threading.main_thread())
            yield {"event": "partial", "text": f"t{i}"}


@pytest.fixture
def asr_stubs(monkeypatch):
    _FakeASRBundle.instances = []
    module = _route_module()
    monkeypatch.setattr(module, "get_tenant_default_model_by_type", lambda s, tid, t: {"llm_name": "asr-m"})
    monkeypatch.setattr(module, "LLMBundle", _FakeASRBundle)
    return _FakeASRBundle


def _post_audio(client, name: str = "a.wav", stream: str | None = None):
    url = "/api/v1/chats/transcriptions" + (f"?stream={stream}" if stream else "")
    return client.post(url, files={"file": (name, b"RIFFfakewav", "audio/wav")})


def test_transcriptions_nonstream_shape(client, asr_stubs):
    resp = _post_audio(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"text": "hello world"}

    (bundle,) = asr_stubs.instances
    assert bundle.seen["db_at_use"] is None  # facade 剥离（变异验证锚点）
    assert bundle.seen["off_loop"] is True  # ASR 同步 HTTP 必须在工作线程执行


def test_transcriptions_stream_frames(client, asr_stubs):
    resp = _post_audio(client, stream="true")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in resp.text.split("\n") if line.startswith("data: ")]
    assert frames == ['data: {"event": "partial", "text": "t0"}', 'data: {"event": "partial", "text": "t1"}']

    (bundle,) = asr_stubs.instances
    assert bundle.seen["stream_threads"] == [True, True]  # 同步流逐项在线程池拉取


def test_transcriptions_rejects_unknown_extension(client, asr_stubs):
    resp = _post_audio(client, name="a.xyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "Unsupported audio format" in body["message"]
    assert asr_stubs.instances == []


def test_transcriptions_surfaces_model_config_error(client, monkeypatch):
    module = _route_module()

    def _boom(s, tid, t):
        raise LookupError("No default speech2text model is set.")

    monkeypatch.setattr(module, "get_tenant_default_model_by_type", _boom)

    resp = _post_audio(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "No default speech2text model" in body["message"]
