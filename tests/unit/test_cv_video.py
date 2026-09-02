import os
import tempfile
from types import SimpleNamespace
from typing import Any

import cv2
import pytest

import core.llm.cv as cv_module
from core.llm.cv import Base, OpenAI_APICV, QWenCV, Zhipu4V


class _FakeCompletions:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class _EncodedFrame:
    def __init__(self, frame_index: int) -> None:
        self.frame_index = frame_index

    def tobytes(self) -> bytes:
        return f"jpeg-{self.frame_index}".encode()


class _FakeCapture:
    def __init__(self, path: str, *, fail_read: bool = False) -> None:
        self.path = path
        self.fail_read = fail_read
        self.current_frame = 0
        self.positions: list[int] = []
        self.released = False

    def get(self, prop: int) -> float:
        assert prop == cv2.CAP_PROP_FRAME_COUNT
        return 100.0

    def set(self, prop: int, value: float) -> bool:
        assert prop == cv2.CAP_PROP_POS_FRAMES
        self.current_frame = int(value)
        self.positions.append(self.current_frame)
        return True

    def read(self) -> tuple[bool, int | None]:
        if self.fail_read:
            return False, None
        return True, self.current_frame

    def release(self) -> None:
        self.released = True


def _response(content: str = "video summary", tokens: int = 11, *, choices: bool = True) -> Any:
    choice_list = [SimpleNamespace(message=SimpleNamespace(content=content))] if choices else []
    return SimpleNamespace(choices=choice_list, usage=SimpleNamespace(total_tokens=tokens))


def _base_model(response: Any, model_type: type[Base] = Base) -> tuple[Base, _FakeCompletions]:
    completions = _FakeCompletions(response)
    model = object.__new__(model_type)
    Base.__init__(model)
    model.model_name = "test-vision-model"
    model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return model, completions


@pytest.mark.parametrize(
    ("system", "history", "kwargs", "expected"),
    [
        ("system", [{"role": "user", "content": "user"}], {"video_prompt": " video ", "prompt": "prompt"}, "video"),
        ("system", [{"role": "user", "content": "user"}], {"prompt": " prompt "}, "prompt"),
        (
            "system",
            [
                {"role": "user", "content": "older"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": [{"type": "text", "text": "latest"}]},
            ],
            {},
            "latest",
        ),
        (" system ", [], {}, "system"),
        (None, [], {}, "Please summarize this video in proper sentences."),
    ],
)
def test_resolve_video_prompt_precedence(
    system: str | None,
    history: list[dict[str, Any]],
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    assert Base()._resolve_video_prompt(system, history, **kwargs) == expected


@pytest.mark.anyio
async def test_openai_compatible_video_chat_sends_three_chronological_jpeg_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    model, completions = _base_model(_response(), OpenAI_APICV)
    extraction_calls: list[tuple[bytes, str]] = []

    def fake_extract(video_bytes: bytes, filename: str = "") -> list[bytes]:
        extraction_calls.append((video_bytes, filename))
        return [b"\xff\xd8first", b"\xff\xd8middle", b"\xff\xd8last"]

    monkeypatch.setattr(Base, "_video_frames_to_image_bytes", staticmethod(fake_extract))

    answer, tokens = await model.async_chat(
        None,
        [{"role": "user", "content": "Describe the actions"}],
        {},
        video_bytes=b"video-bytes",
        filename="sample.mp4",
    )

    assert (answer, tokens) == ("video summary", 11)
    assert extraction_calls == [(b"video-bytes", "sample.mp4")]
    assert len(completions.calls) == 1
    messages = completions.calls[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content[0]["type"] == "text"
    assert "chronological order" in content[0]["text"]
    assert content[0]["text"].endswith("Describe the actions")
    assert [item["type"] for item in content[1:]] == ["image_url", "image_url", "image_url"]
    assert [item["image_url"]["url"] for item in content[1:]] == [
        Base.image2base64(b"\xff\xd8first"),
        Base.image2base64(b"\xff\xd8middle"),
        Base.image2base64(b"\xff\xd8last"),
    ]


@pytest.mark.anyio
async def test_base_chat_without_video_keeps_existing_message_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    model, completions = _base_model(_response(content="image summary", tokens=5))

    def fail_if_called(video_bytes: bytes, filename: str = "") -> list[bytes]:
        raise AssertionError(f"unexpected video extraction for {filename}: {len(video_bytes)}")

    monkeypatch.setattr(Base, "_video_frames_to_image_bytes", staticmethod(fail_if_called))

    answer, tokens = await model.async_chat(
        "system",
        [{"role": "user", "content": "Describe the image"}],
        {},
        images=[b"\x89PNG\r\n\x1a\n"],
    )

    assert (answer, tokens) == ("image summary", 5)
    assert [message["role"] for message in completions.calls[0]["messages"]] == ["system", "user"]


def test_video_frame_extraction_uses_one_temp_file_and_three_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    real_named_temporary_file = tempfile.NamedTemporaryFile
    temp_paths: list[str] = []
    captures: list[_FakeCapture] = []

    def tracked_temp_file(*args: Any, **kwargs: Any) -> Any:
        tmp = real_named_temporary_file(*args, **kwargs)
        temp_paths.append(tmp.name)
        return tmp

    def fake_video_capture(path: str) -> _FakeCapture:
        capture = _FakeCapture(path)
        captures.append(capture)
        return capture

    def fake_imencode(extension: str, frame_index: int) -> tuple[bool, _EncodedFrame]:
        assert extension == ".jpg"
        return True, _EncodedFrame(frame_index)

    monkeypatch.setattr(cv_module.tempfile, "NamedTemporaryFile", tracked_temp_file)
    monkeypatch.setattr(cv2, "VideoCapture", fake_video_capture)
    monkeypatch.setattr(cv2, "imencode", fake_imencode)

    frames = Base._video_frames_to_image_bytes(b"video", "clip.mov")

    assert frames == [b"jpeg-10", b"jpeg-50", b"jpeg-90"]
    assert len(temp_paths) == 1
    assert len(captures) == 1
    assert captures[0].path == temp_paths[0]
    assert captures[0].positions == [10, 50, 90]
    assert captures[0].released is True
    assert not os.path.exists(temp_paths[0])


def test_video_frame_extraction_cleans_up_after_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    real_named_temporary_file = tempfile.NamedTemporaryFile
    temp_paths: list[str] = []
    captures: list[_FakeCapture] = []

    def tracked_temp_file(*args: Any, **kwargs: Any) -> Any:
        tmp = real_named_temporary_file(*args, **kwargs)
        temp_paths.append(tmp.name)
        return tmp

    def fake_video_capture(path: str) -> _FakeCapture:
        capture = _FakeCapture(path, fail_read=True)
        captures.append(capture)
        return capture

    monkeypatch.setattr(cv_module.tempfile, "NamedTemporaryFile", tracked_temp_file)
    monkeypatch.setattr(cv2, "VideoCapture", fake_video_capture)

    with pytest.raises(RuntimeError, match="Failed to extract a frame"):
        Base._video_frames_to_image_bytes(b"broken-video", "clip.mp4")

    assert len(temp_paths) == 1
    assert captures[0].released is True
    assert not os.path.exists(temp_paths[0])


@pytest.mark.anyio
async def test_qwen_video_keeps_native_full_video_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, str, str]] = []

    def fake_process_video(self: QWenCV, video_bytes: bytes, filename: str, prompt: str) -> tuple[str, int]:
        calls.append((video_bytes, filename, prompt))
        return "native summary", 13

    async def fail_generic_path(self: Base, video_bytes: bytes, filename: str, prompt: str) -> tuple[str, int]:
        raise AssertionError("QWen video must not use the generic frame path")

    monkeypatch.setattr(QWenCV, "_process_video", fake_process_video)
    monkeypatch.setattr(Base, "_describe_video_frame", fail_generic_path)
    model = object.__new__(QWenCV)

    result = await model.async_chat(
        None,
        [],
        {},
        video_bytes=b"full-video",
        filename="native.mp4",
        video_prompt="native prompt",
    )

    assert result == ("native summary", 13)
    assert calls == [(b"full-video", "native.mp4", "native prompt")]


@pytest.mark.anyio
async def test_zhipu_video_uses_frame_path_and_cleans_box_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, str, str]] = []

    async def fake_describe(self: Base, video_bytes: bytes, filename: str, prompt: str) -> tuple[str, int]:
        calls.append((video_bytes, filename, prompt))
        return "<|begin_of_box|>visible action<|end_of_box|>", 17

    monkeypatch.setattr(Base, "_describe_video_frame", fake_describe)
    model = object.__new__(Zhipu4V)

    result = await model.async_chat(
        "system",
        [{"role": "user", "content": "Describe it"}],
        {},
        video_bytes=b"video",
        filename="zhipu.mp4",
    )

    assert result == ("visible action", 17)
    assert calls == [(b"video", "zhipu.mp4", "Describe it")]


@pytest.mark.anyio
async def test_base_video_chat_returns_error_for_extraction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    model, completions = _base_model(_response())

    def fail_extract(video_bytes: bytes, filename: str = "") -> list[bytes]:
        raise RuntimeError(f"cannot decode {filename}: {len(video_bytes)} bytes")

    monkeypatch.setattr(Base, "_video_frames_to_image_bytes", staticmethod(fail_extract))

    answer, tokens = await model.async_chat(None, [], {}, video_bytes=b"broken", filename="broken.mp4")

    assert answer == "**ERROR**: cannot decode broken.mp4: 6 bytes"
    assert tokens == 0
    assert completions.calls == []


@pytest.mark.anyio
async def test_base_video_chat_returns_error_for_empty_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    model, completions = _base_model(_response(choices=False))

    def fake_extract(video_bytes: bytes, filename: str = "") -> list[bytes]:
        return [b"\xff\xd8one", b"\xff\xd8two", b"\xff\xd8three"]

    monkeypatch.setattr(Base, "_video_frames_to_image_bytes", staticmethod(fake_extract))

    answer, tokens = await model.async_chat(None, [], {}, video_bytes=b"video", filename="empty.mp4")

    assert answer == "**ERROR**: LLM returned empty response"
    assert tokens == 0
    assert len(completions.calls) == 1
