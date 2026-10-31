"""The real Fish adapter must yield audio and close streams on probe exit."""

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import ormsgpack
import pytest

from api.db.model_contract import ModelRegistration
from api.db.services.provider_verification import verify_model
from tests.support.resource_journal import ResourceJournal

VOICE = "1234567890abcdef12345678"


@pytest.mark.parametrize("mode", ["audio", "empty", "rejected"])
async def test_fish_probe_requires_nonempty_audio_and_closes_real_adapter(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    clients: list[httpx.Client] = []
    closed: list[bool] = []
    requests: list[httpx.Request] = []
    tails: list[bool] = []
    client_init = httpx.Client.__init__

    class Audio(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"ID3-audio" if mode == "audio" else b""
            tails.append(True)

        def close(self) -> None:
            closed.append(True)

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://api.fish.audio/v1/tts"
        assert request.headers["api-key"] == "synthetic-fish-key"
        assert request.headers["content-type"] == "application/msgpack"
        payload = ormsgpack.unpackb(request.content)
        assert payload["reference_id"] == VOICE and payload["text"] == "Connection test"
        return httpx.Response(401 if mode == "rejected" else 200, stream=Audio())

    def initialize(client: httpx.Client, **kwargs: Any) -> None:
        client_init(client, transport=httpx.MockTransport(transport), **kwargs)
        clients.append(client)

    monkeypatch.setattr(httpx.Client, "__init__", initialize)
    declaration = ModelRegistration(model_name=VOICE, model_types=["tts"], max_tokens=0)
    if mode == "audio":
        assert await verify_model("Fish Audio", "synthetic-fish-key", "", declaration) == {"verified": True, "model_name": VOICE, "model_type": "tts"}
        assert tails == []  # A connection probe stops after the first nonempty audio chunk.
    else:
        with pytest.raises(ValueError, match="Provider connection verification failed") as error:
            await verify_model("Fish Audio", "synthetic-fish-key", "", declaration)
        assert "synthetic-fish-key" not in str(error.value)
    assert len(requests) == 1 and closed == [True]
    assert len(clients) == 1 and clients[0].is_closed


async def test_fish_probe_cancellation_drains_sync_work_before_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    journal = ResourceJournal("fish-synchronous-probe-cancellation")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    closed = threading.Event()
    clients: list[httpx.Client] = []
    client_init = httpx.Client.__init__

    class Audio(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            journal.event("synthesis-thread-entered", native_id=threading.get_native_id())
            loop.call_soon_threadsafe(entered.set)
            assert released.wait(5), "Controlled synthesis was not released"
            yield b"ID3-audio"

        def close(self) -> None:
            closed.set()

    def initialize(client: httpx.Client, **kwargs: Any) -> None:
        client_init(client, transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=Audio())), **kwargs)
        clients.append(client)

    monkeypatch.setattr(httpx.Client, "__init__", initialize)
    journal.event("reserve-synchronous-synthesis", transport="in-memory httpx boundary", operation="owned verify_model to_thread")
    task = asyncio.create_task(verify_model("Fish Audio", "synthetic-fish-key", "", ModelRegistration(model_name=VOICE, model_types=["tts"], max_tokens=0)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        released.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set() and clients and all(item.is_closed for item in clients)
    journal.event("synthesis-independent-readback", task_done=task.done(), audio_stream_closed=closed.is_set(), clients_closed=all(item.is_closed for item in clients))
