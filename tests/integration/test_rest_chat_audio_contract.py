"""Real REST audio, SQL model selection, provider failures and temporary cleanup."""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Tenant
from tests.support.rest_chat_contract import RestChatContract, sse_frames
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


@pytest.fixture
def audio_provider(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    import core.llm as providers

    ctx = rest_chat_contract
    state: dict[str, Any] = {"failure": None, "paths": [], "fds": [], "asr": [], "tts": []}
    record = ctx.runtime["record"]["rest_audio"] = {"paths": []}

    def save_record() -> None:
        ctx.runtime["record_path"].write_text(json.dumps(ctx.runtime["record"]))

    def create_temp(suffix: str) -> tuple[int, str]:
        path = tmp_path / f"owned-{uuid4().hex}{suffix}"
        record["paths"].append(str(path))
        save_record()
        state["paths"].append(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        stat = os.fstat(fd)
        state["fds"].append((fd, stat.st_dev, stat.st_ino))
        return fd, str(path)

    def assert_clean() -> None:
        assert all(not path.exists() for path in state["paths"]), state["paths"]
        for fd, device, inode in state["fds"]:
            try:
                current = os.fstat(fd)
            except OSError:
                continue
            # The runtime may reuse a closed descriptor for a connection.
            assert (current.st_dev, current.st_ino) != (device, inode)

    class Speech:
        def __init__(self, key: str, model: str, **kwargs: Any) -> None:
            assert key == "controlled" and model == "speech2text"
            if state["failure"] == "constructor":
                raise RuntimeError("Controlled audio constructor failure")

        def transcription(self, path: str) -> tuple[str, int]:
            state["asr"].append(Path(path).read_bytes())
            if state["failure"] == "provider":
                raise RuntimeError("Controlled transcription failure")
            return "语音文字", 0

        def stream_transcription(self, path: str) -> Iterator[dict[str, Any]]:
            state["asr"].append(Path(path).read_bytes())
            yield {"event": "partial", "text": "语音"}
            if state["failure"] == "provider":
                raise RuntimeError("Controlled transcription failure")
            yield {"event": "final", "text": "语音文字"}

    class Voice:
        def __init__(self, key: str, model: str, **kwargs: Any) -> None:
            assert key == "controlled" and model == "tts"

        def tts(self, text: str) -> Iterator[bytes | int]:
            state["tts"].append(text)
            if state["failure"] == "provider":
                raise RuntimeError("Controlled speech failure")
            yield f"audio:{text}|".encode()
            yield 0

    def cleanup() -> None:
        leftovers = [str(path) for path in state["paths"] if path.exists()]
        record["leftovers_before_cleanup"] = leftovers
        for path in state["paths"]:
            path.unlink(missing_ok=True)
        record["remaining"] = [str(path) for path in state["paths"] if path.exists()]
        save_record()
        assert not record["remaining"]

    monkeypatch.setitem(providers.Seq2txtModel, ctx.factory, Speech)
    monkeypatch.setitem(providers.TTSModel, ctx.factory, Voice)
    monkeypatch.setattr(sys.modules["api.apps.restful_apis.chat"], "tempfile", SimpleNamespace(mkstemp=create_temp))
    state["assert_clean"] = assert_clean
    with ExitStack() as cleanup_stack:
        cleanup_stack.callback(cleanup)
        yield state


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_transcription_success_failure_and_cleanup(rest_chat_contract: RestChatContract, audio_provider: dict[str, Any], stream: bool, credential: str) -> None:
    ctx, audio = rest_chat_contract, audio_provider
    before = ctx.snapshot()
    path = "/chat/audio/transcription"

    def transcribe(**kwargs: Any) -> Any:
        return ctx.request("POST", path, credential=credential, params={"stream": str(stream).lower()}, files={"file": ("sample.wav", b"controlled-audio", "audio/wav")}, **kwargs)

    assert ctx.request("POST", path, credential=credential).status_code == 422
    invalid = ctx.request("POST", path, credential=credential, files={"file": ("bad.txt", b"bad", "text/plain")})
    assert invalid.status_code == 200 and invalid.json()["code"] != 0
    assert ctx.request("POST", path, credential=None, files={"file": ("sample.wav", b"denied", "audio/wav")}).status_code == 401
    assert audio["paths"] == [] and audio["asr"] == []
    response = transcribe()
    assert response.status_code == 200
    if stream:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert sse_frames(response) == [{"event": "partial", "text": "语音"}, {"event": "final", "text": "语音文字"}]
    else:
        assert response.json()["code"] == 0 and response.json()["data"]["text"] == "语音文字"
    assert audio["asr"] == [b"controlled-audio"]
    audio["assert_clean"]()

    owner = ctx.runtime["owners"][credential == "api_key"]
    with Session(ctx.runtime["engine"]) as db:
        db.get(Tenant, owner).asr_id = "missing-model"
        db.commit()
    response = transcribe()
    assert response.status_code == 200 and response.json()["code"] != 0
    assert audio["asr"] == [b"controlled-audio"]
    audio["assert_clean"]()
    with Session(ctx.runtime["engine"]) as db:
        db.get(Tenant, owner).asr_id = f"speech2text@{ctx.factory}"
        db.commit()
    for failure in ("constructor", "provider"):
        audio["failure"] = failure
        response = transcribe()
        if stream and failure == "provider":
            events = sse_frames(response)
            assert events[-1]["event"] == "error" and not any(event.get("event") == "final" for event in events)
        else:
            assert response.status_code == 500 or response.json().get("code", response.json().get("retcode")) != 0
        audio["assert_clean"]()
    assert ctx.snapshot() == before


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_speech_stream_and_failure_contract(rest_chat_contract: RestChatContract, audio_provider: dict[str, Any], credential: str) -> None:
    ctx, audio = rest_chat_contract, audio_provider
    before = ctx.snapshot()
    path = "/chat/audio/speech"
    assert ctx.request("POST", path, credential=None, json={"text": "denied"}).status_code == 401
    assert ctx.request("POST", path, credential=credential, json={"text": []}).status_code == 422
    assert audio["tts"] == []
    response = ctx.request("POST", path, credential=credential, json={"text": "第一句。第二句！"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    assert response.content == "audio:第一句|audio:第二句|".encode() and audio["tts"] == ["第一句", "第二句"]
    audio["failure"] = "provider"
    failed = ctx.request("POST", path, credential=credential, json={"text": "失败"})
    error = sse_frames(failed)
    assert error[0]["code"] == 500 and "Controlled speech failure" in error[0]["message"]
    assert ctx.snapshot() == before
