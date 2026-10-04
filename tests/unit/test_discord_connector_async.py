from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest
from discord import LoginFailure, MessageType

from common.data_source import discord_connector as discord
from common.data_source.exceptions import ConnectorMissingCredentialError
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env, task  # noqa: F401

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class FakeChannel:
    def __init__(self, name: str = "selected", *, server_id: int = 1) -> None:
        self.name = name
        self.guild = SimpleNamespace(id=server_id, me=object())
        self.messages: list[SimpleNamespace] = []
        self.threads: list[FakeThread] = []
        self.archived: list[FakeThread] = []
        self.history_calls: list[dict[str, Any]] = []
        self.read_ids: list[int] = []
        self.failure: Exception | None = None

    def permissions_for(self, member: object) -> SimpleNamespace:
        return SimpleNamespace(read_message_history=True)

    async def history(self, *, limit: int | None, after: datetime | None, before: datetime | None) -> AsyncIterator[SimpleNamespace]:
        self.history_calls.append({"limit": limit, "after": after, "before": before})
        for message in self.messages:
            if after and message.created_at <= after:
                continue
            if before and message.created_at >= before:
                continue
            self.read_ids.append(message.id)
            yield message
        if self.failure:
            raise self.failure

    async def archived_threads(self, *, limit: int | None) -> AsyncIterator[FakeThread]:
        assert limit is None
        for thread in self.archived:
            yield thread


class FakeThread(FakeChannel):
    pass


class FakeClient:
    def __init__(self, source: DiscordSource) -> None:
        self.source = source
        self.ready = asyncio.Event()
        self.closed = False
        self.start_finished = False
        self.ready_finished = False
        self.heartbeat = threading.Event()
        self.thread_id = threading.get_ident()
        self.loop = asyncio.get_running_loop()

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        await self.close()

    async def start(self, token: str) -> None:
        assert token == "controlled-test-token"
        try:
            if self.source.login_failure:
                raise self.source.login_failure
            if self.source.stop_before_ready:
                return
            self.ready.set()
            asyncio.get_running_loop().call_later(0.01, self.heartbeat.set)
            await asyncio.Event().wait()
        finally:
            self.start_finished = True

    async def wait_until_ready(self) -> None:
        try:
            # Bound broken-start regressions so a failure cannot strand a test thread.
            await asyncio.wait_for(self.ready.wait(), timeout=2)
        finally:
            self.ready_finished = True

    def get_all_channels(self) -> list[FakeChannel]:
        return self.source.channels

    async def close(self) -> None:
        self.closed = True


class DiscordSource:
    def __init__(self) -> None:
        self.channels = [FakeChannel()]
        self.clients: list[FakeClient] = []
        self.login_failure: Exception | None = None
        self.stop_before_ready = False

    def client(self, **kwargs: Any) -> FakeClient:
        client = FakeClient(self)
        self.clients.append(client)
        return client

    def add_message(self, message_id: int, channel: FakeChannel | None = None) -> None:
        channel = channel or self.channels[0]
        channel.messages.append(
            SimpleNamespace(
                id=message_id,
                channel=channel,
                type=MessageType.default,
                content=f"message {message_id}",
                author=SimpleNamespace(name="author"),
                created_at=BASE_TIME + timedelta(seconds=message_id),
                edited_at=None,
                jump_url=f"https://discord.invalid/messages/{message_id}",
            )
        )

    def assert_closed(self) -> None:
        assert self.clients
        for client in self.clients:
            assert client.closed and client.start_finished and client.ready_finished
            assert client.loop.is_closed()
            assert all(thread.ident != client.thread_id for thread in threading.enumerate())


@pytest.fixture
def discord_source(monkeypatch: pytest.MonkeyPatch) -> DiscordSource:
    source = DiscordSource()
    monkeypatch.setattr(discord, "Client", source.client)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(discord, "Thread", FakeThread)
    return source


def connector(*, batch_size: int = 2) -> discord.DiscordConnector:
    value = discord.DiscordConnector(batch_size=batch_size)
    value.load_credentials({"discord_bot_token": "controlled-test-token"})
    return value


@pytest.mark.parametrize("mode", ["full", "incremental"])
@pytest.mark.parametrize("scope", [{"server_ids": [" 1 "], "channels": [" selected "]}, {"server_ids": " 1, ", "channel_names": "selected,"}])
async def test_async_driver_reads_existing_merged_documents(discord_source: DiscordSource, mode: str, scope: dict[str, Any]) -> None:
    for message_id in range(1, 6):
        discord_source.add_message(message_id)
    other_channel = FakeChannel("unselected")
    other_server = FakeChannel(server_id=2)
    discord_source.channels.extend([other_channel, other_server])
    discord_source.add_message(6, other_channel)
    discord_source.add_message(7, other_server)
    sync = sync_module.Discord({**scope, "batch_size": 2, "credentials": {"discord_bot_token": "controlled-test-token"}})
    current = task()
    if mode == "full":
        current["poll_range_start"] = None

    batches = list(await sync._generate(current))

    assert [[doc.id for doc in batch] for batch in batches] == [["DISCORD_1"], ["DISCORD_3"], ["DISCORD_5"]]
    assert [batch[0].blob for batch in batches] == [b"\n\nmessage 1\n\nmessage 2", b"\n\nmessage 3\n\nmessage 4", b"\n\nmessage 5"]
    assert [batch[0].doc_updated_at for batch in batches] == [BASE_TIME + timedelta(seconds=i) for i in (2, 4, 5)]
    assert discord_source.clients[0].thread_id != threading.get_ident()
    assert not other_channel.history_calls and not other_server.history_calls
    discord_source.assert_closed()


def test_sync_caller_preserves_scope_threads_and_time_window(discord_source: DiscordSource) -> None:
    selected = discord_source.channels[0]
    active = FakeThread("active")
    archived = FakeThread("archived")
    selected.threads.append(active)
    selected.archived.append(archived)
    other_channel = FakeChannel("other")
    other_server = FakeChannel(server_id=2)
    discord_source.channels.extend([other_channel, other_server])
    for message_id in range(1, 206):
        discord_source.add_message(message_id)
    discord_source.add_message(206, active)
    discord_source.add_message(207, archived)
    discord_source.add_message(208, other_channel)
    discord_source.add_message(209, other_server)
    value = connector(batch_size=1)
    value.channel_names = ["selected"]
    value.server_ids = [1]

    docs = [batch[0] for batch in value.poll_source((BASE_TIME + timedelta(seconds=2)).timestamp(), (BASE_TIME + timedelta(seconds=208)).timestamp())]

    assert [doc.id for doc in docs] == [f"DISCORD_{i}" for i in range(3, 208)]
    assert not other_channel.history_calls and not other_server.history_calls
    for channel in (selected, active, archived):
        assert channel.history_calls == [{"limit": None, "after": BASE_TIME + timedelta(seconds=2), "before": BASE_TIME + timedelta(seconds=208)}]
    discord_source.assert_closed()


@pytest.mark.parametrize("failure", [LoginFailure("invalid credentials"), RuntimeError("gateway startup failed")])
async def test_login_failure_propagates_without_waiting_forever(discord_source: DiscordSource, failure: Exception) -> None:
    discord_source.login_failure = failure
    with pytest.raises(type(failure), match=str(failure)):
        list(connector().load_from_state())
    discord_source.assert_closed()


def test_client_stopping_before_ready_is_failure(discord_source: DiscordSource) -> None:
    discord_source.stop_before_ready = True
    with pytest.raises(RuntimeError, match="stopped before becoming ready"):
        list(connector().load_from_state())
    discord_source.assert_closed()


def test_later_history_failure_is_not_a_successful_partial_read(discord_source: DiscordSource) -> None:
    discord_source.add_message(1)
    discord_source.add_message(2)
    discord_source.channels[0].failure = PermissionError("history page denied")
    batches = connector().load_from_state()
    assert next(batches)[0].id == "DISCORD_1"
    with pytest.raises(PermissionError, match="history page denied"):
        next(batches)
    discord_source.assert_closed()


def test_early_consumer_close_stops_client_without_reading_ahead(discord_source: DiscordSource) -> None:
    for message_id in range(1, 6):
        discord_source.add_message(message_id)
    batches = connector().load_from_state()
    assert next(batches)[0].id == "DISCORD_1"
    assert discord_source.channels[0].read_ids == [1, 2]
    assert discord_source.clients[0].heartbeat.wait(timeout=1)
    batches.close()
    discord_source.assert_closed()


@pytest.mark.parametrize("token", [None, "", "controlled-test-token"])
def test_settings_validation_checks_credential_presence(token: str | None) -> None:
    value = discord.DiscordConnector()
    value.load_credentials({"discord_bot_token": token})
    if token:
        value.validate_connector_settings()
    else:
        with pytest.raises(ConnectorMissingCredentialError):
            value.validate_connector_settings()


@pytest.mark.parametrize("channels", [None, "", [], ["selected"]])
async def test_channel_key_precedence_and_legacy_fallback(channels: Any) -> None:
    sync = sync_module.Discord({"channels": channels, "channel_names": "legacy", "server_ids": [1], "credentials": {"discord_bot_token": "controlled-test-token"}})
    await sync._generate(task())
    assert sync.connector.channel_names == (["selected"] if channels else ["legacy"])
    assert sync.connector.server_ids == [1]
    assert sync.connector.batch_size == 1024


@pytest.mark.parametrize("scope", [{"server_ids": ["invalid"]}, {"server_ids": ["1", "invalid"]}, {"server_ids": {}}, {"channels": [True]}])
async def test_invalid_scope_fails_before_remote_access(discord_source: DiscordSource, scope: dict[str, Any]) -> None:
    sync = sync_module.Discord({**scope, "credentials": {"discord_bot_token": "controlled-test-token"}})
    with pytest.raises(ValueError):
        await sync._generate(task())
    assert not discord_source.clients


@pytest.mark.parametrize("failure", ["none", "read", "ingestion"])
async def test_sync_never_prunes_discord_even_with_saved_flag(discord_source: DiscordSource, sync_env: dict[str, Any], failure: str) -> None:
    discord_source.add_message(1)
    if failure == "read":
        discord_source.channels[0].failure = PermissionError("history denied")
    ingested: list[str] = []

    def ingest(*args: Any) -> tuple[list[str], list[str]]:
        ingested.extend(doc["blob"].decode() for doc in args[2])
        return (["upload failed"], []) if failure == "ingestion" else ([], ["local-doc"])

    sync_env["monkeypatch"].setattr(sync_module.SyncLogsService, "duplicate_and_parse", ingest)
    sync = sync_module.Discord({"sync_deleted_files": True, "batch_size": 2, "credentials": {"discord_bot_token": "controlled-test-token"}})

    await sync(task())

    assert sync_env["calls"] == ["start", "complete" if failure == "none" else "fail"]
    assert ingested == ([] if failure == "read" else ["\n\nmessage 1"])
    discord_source.assert_closed()
