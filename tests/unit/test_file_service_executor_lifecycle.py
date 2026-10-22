"""File parsing must return or fail only after its owned threads have stopped."""

import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Any

import pytest

from api.db.services import file_service
from api.db.services.file_service import FileService


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ThreadPoolExecutor]]:
    owned: list[ThreadPoolExecutor] = []

    class OwnedPool(ThreadPoolExecutor):
        def __init__(self, max_workers: int) -> None:
            super().__init__(max_workers=max_workers, thread_name_prefix="file-lifecycle-test")
            owned.append(self)

    monkeypatch.setattr(file_service, "ThreadPoolExecutor", OwnedPool)
    yield owned
    for pool in owned:
        pool.shutdown(wait=True, cancel_futures=True)


def assert_closed(pools: list[ThreadPoolExecutor]) -> None:
    assert pools
    for pool in pools:
        assert all(not thread.is_alive() for thread in pool._threads)
        with pytest.raises(RuntimeError, match="shutdown"):
            pool.submit(lambda: None)


def descriptor(name: str, mime: str = "text/plain") -> dict[str, Any]:
    return {"id": name, "name": name, "mime_type": mime, "created_by": "owner"}


def test_parse_docs_keeps_input_order_and_owner(monkeypatch: pytest.MonkeyPatch, pools: list[ThreadPoolExecutor]) -> None:
    second_finished = threading.Event()
    calls: list[tuple[str, bytes, bool, str]] = []

    def parse(name: str, blob: bytes, img_base64: bool, user_id: str) -> str:
        if name == "first.txt":
            assert second_finished.wait(5)
        else:
            second_finished.set()
        calls.append((name, blob, img_base64, user_id))
        return blob.decode()

    monkeypatch.setattr(FileService, "parse", staticmethod(parse))
    assert FileService.parse_docs([(b"first", "first.txt"), (b"second", "second.txt")], "owner") == "first\n\nsecond"
    assert sorted(calls) == [("first.txt", b"first", False, "owner"), ("second.txt", b"second", False, "owner")]
    assert_closed(pools)


@pytest.mark.parametrize("raw", [False, True])
def test_get_files_keeps_mixed_file_contract(monkeypatch: pytest.MonkeyPatch, pools: list[ThreadPoolExecutor], raw: bool) -> None:
    calls: list[tuple[Any, ...]] = []
    blobs = {"a.txt": b"a", "pic.png": b"image", "b.txt": b"b"}
    monkeypatch.setattr(FileService, "get_blob", staticmethod(lambda user, key: blobs[key] if user == "owner" else pytest.fail("wrong owner")))

    def parse(*args: Any) -> str:
        calls.append(args)
        return args[1].decode()

    monkeypatch.setattr(FileService, "parse", staticmethod(parse))
    files = [descriptor("a.txt"), descriptor("pic.png", "image/png"), descriptor("b.txt")]
    result = FileService.get_files(files, raw=raw, layout_recognize="Plain Text")
    assert result == (["a", "b"], [b"image"]) if raw else result == ["a", "data:image/png;base64,aW1hZ2U=", "b"]
    assert sorted(calls) == [("a.txt", b"a", True, "owner", "Plain Text"), ("b.txt", b"b", True, "owner", "Plain Text")]
    assert_closed(pools)


def test_empty_inputs(pools: list[ThreadPoolExecutor]) -> None:
    assert FileService.parse_docs([], "owner") == ""
    assert FileService.get_files(None) == []
    assert FileService.get_files([], raw=True) == ([], [])
    assert_closed(pools)


@pytest.mark.parametrize("method", ["parse_docs", "get_files"])
@pytest.mark.parametrize("failure", ["result", "submit", "input"])
def test_failure_waits_for_other_running_work(monkeypatch: pytest.MonkeyPatch, pools: list[ThreadPoolExecutor], method: str, failure: str) -> None:
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def parse(name: str, *args: Any) -> str:
        if name == "broken.txt":
            raise ValueError("parse failure")
        started.set()
        try:
            assert release.wait(5)
            return "slow"
        finally:
            stopped.set()

    monkeypatch.setattr(FileService, "parse", staticmethod(parse))

    def blob(user: str, key: str) -> bytes:
        if failure == "input" and key == "broken.txt":
            assert started.wait(5)
            raise ValueError("input failure")
        return key.encode()

    monkeypatch.setattr(FileService, "get_blob", staticmethod(blob))
    if failure == "submit":
        submit = file_service.ThreadPoolExecutor.submit

        def submit_or_fail(self: ThreadPoolExecutor, fn: Any, *args: Any, **kwargs: Any) -> Future[Any]:
            if args and args[0] == "broken.txt":
                assert started.wait(5)
                raise ValueError("submit failure")
            return submit(self, fn, *args, **kwargs)

        monkeypatch.setattr(file_service.ThreadPoolExecutor, "submit", submit_or_fail)

    class BrokenInput(list[tuple[bytes, str]]):
        def __iter__(self) -> Iterator[tuple[bytes, str]]:
            yield b"slow", "slow.txt"
            assert started.wait(5)
            raise ValueError("input failure")

    names = ["broken.txt", "slow.txt"] if failure == "result" else ["slow.txt", "broken.txt"]
    data = BrokenInput() if failure == "input" else [(name.encode(), name) for name in names]
    # The outer pool belongs to the caller and is intentionally outside the factory under test.
    with ThreadPoolExecutor(max_workers=1) as caller:
        work = caller.submit(FileService.parse_docs, data, "owner") if method == "parse_docs" else caller.submit(FileService.get_files, [descriptor(name) for name in names])
        try:
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                work.result(timeout=0.05)
            assert not stopped.is_set()
        finally:
            release.set()
        with pytest.raises(ValueError, match=f"{'parse' if failure == 'result' else failure} failure"):
            work.result(timeout=5)
    assert stopped.is_set()
    assert_closed(pools)


@pytest.mark.parametrize("raw", [False, True])
def test_dialog_consumer_with_real_text_parser(monkeypatch: pytest.MonkeyPatch, pools: list[ThreadPoolExecutor], raw: bool) -> None:
    from api.db.services.dialog_service import split_file_attachments

    monkeypatch.setattr(FileService, "get_blob", staticmethod(lambda user, key: b"thread lifecycle reference" if key == "note.txt" else b"image"))
    text, images = split_file_attachments([descriptor("note.txt"), descriptor("image.png", "image/png")], raw=raw)
    assert len(text) == 1 and "thread lifecycle reference" in text[0] and "note.txt" in text[0]
    assert images == ([b"image"] if raw else ["data:image/png;base64,aW1hZ2U="])
    assert_closed(pools)


def test_reference_consumer_with_real_text_parser(pools: list[ThreadPoolExecutor]) -> None:
    from api.db.services.reference_service import ReferenceService

    result = ReferenceService.parse_file_content(b"reference parsing remains available", "reference.txt")
    assert "reference parsing remains available" in result and "reference.txt" in result
    assert_closed(pools)
