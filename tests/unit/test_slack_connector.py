from collections.abc import Iterator
from typing import Any

import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from common.data_source import slack_connector as module
from common.data_source.interfaces import StaticCredentialsProvider
from tests.support.connector_sdk_fakes import SlackHTTP
from tests.support.resource_journal import ResourceJournal


@pytest.fixture(autouse=True)
def case_journal(request: pytest.FixtureRequest) -> Iterator[None]:
    journal = ResourceJournal(request.node.nodeid)
    yield
    journal.event("case-complete", external_resources=[])


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> tuple[module.SlackConnector, SlackHTTP]:
    http = SlackHTTP()
    monkeypatch.setattr(WebClient, "_sync_send", lambda client, api_url, req_args: http(client, api_url, req_args))
    connector = module.SlackConnector(batch_size=1)
    connector.set_credentials_provider(StaticCredentialsProvider("tenant", "slack", {"slack_bot_token": "controlled-token"}))
    return connector, http


def test_full_sync_utf8_thread_dedup_pagination(slack: tuple[Any, Any]) -> None:
    connector, http = slack
    docs = [doc for batch in connector.load_from_state() for doc in batch]
    assert [doc.id for doc in docs] == ["C1__1767225600.0", "C1__1767225601.0"]
    assert docs[0].blob == "古い親\n\n新回复".encode()
    assert docs[0].size_bytes == len(docs[0].blob) and docs[0].extension == ".txt"
    assert docs[0].semantic_identifier == "Alice in #general: 古い親"
    assert docs[0].metadata == {"Channel": "general"}
    calls = [item for item in http.calls if item["method"] == "conversations.replies"]
    assert len(calls) == 2 and calls[1]["params"]["cursor"] == "replies-2"
    assert len([item for item in http.calls if item["method"] == "conversations.list"]) == 2


def test_incremental_catches_new_replies_to_old_parents(slack: tuple[Any, Any]) -> None:
    connector, http = slack
    docs = [doc for batch in connector.poll_source(1767225602.0, 1767312000.0) for doc in batch]
    assert [doc.id for doc in docs] == ["C1__1767225600.0"]
    history = [item["params"] for item in http.calls if item["method"] == "conversations.history"]
    assert all("oldest" not in params and params["latest"] == "1767312000.0" for params in history)
    assert list(connector.poll_source(1767312000.0, 1767312001.0)) == []


def test_null_and_explicit_empty_scopes_remain_distinct(slack: tuple[Any, Any]) -> None:
    connector, http = slack
    connector.channels = []
    assert list(connector.load_from_state()) == [] and http.calls == []
    connector.channels = ["#general"]
    assert connector.channels == ["general"]
    assert len(list(connector.load_from_state())) == 2
    connector.channels = ["missing"]
    with pytest.raises(ValueError, match="not found"):
        list(connector.load_from_state())


@pytest.mark.parametrize("mode", ["permission", "later-page", "thread-error", "repeat", "malformed"])
def test_permission_page_and_thread_errors_fail_closed(slack: tuple[Any, Any], mode: str) -> None:
    connector, http = slack
    http.mode = mode
    with pytest.raises((SlackApiError, module.ConnectorValidationError)):
        list(connector.load_from_state())
    if mode == "permission":
        assert len(http.calls) == 1  # Never fall back to public-only enumeration.


def test_slim_uses_canonical_thread_ids(slack: tuple[Any, Any]) -> None:
    connector, _ = slack
    assert [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch] == ["C1__1767225600.0", "C1__1767225601.0"]


def test_cancellation_keeps_original_exception(slack: tuple[Any, Any]) -> None:
    connector, http = slack
    stopped = False

    def cancel(method: str) -> None:
        nonlocal stopped
        if method == "conversations.replies":
            stopped = True

    def check() -> None:
        if stopped:
            raise InterruptedError("controlled cancellation")

    connector._check_active = check
    http.after_request = cancel
    with pytest.raises(InterruptedError):
        list(connector.load_from_state())
    assert len([item for item in http.calls if item["method"] == "conversations.replies"]) == 1


@pytest.mark.parametrize("channels", [False, {}, "general", [None], [""], ["#"]])
def test_invalid_channel_config_is_rejected(channels: Any) -> None:
    with pytest.raises(module.ConnectorValidationError):
        module.SlackConnector(channels=channels)


def test_edits_advance_document_update_time(slack: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    connector, _ = slack
    original = module.get_channel_messages

    def messages(*args: Any, **kwargs: Any) -> Iterator[list[dict[str, Any]]]:
        for batch in original(*args, **kwargs):
            for message in batch:
                if message["ts"] == "1767225601.0":
                    message["edited"] = {"ts": "1767398400.0"}
                    message["text"] = "edited old message"
            yield batch

    monkeypatch.setattr(module, "get_channel_messages", messages)
    docs = [doc for batch in connector.poll_source(1767312000.0, 1767398400.0) for doc in batch]
    assert [doc.id for doc in docs] == ["C1__1767225601.0"]
    assert docs[0].blob == b"edited old message"


@pytest.mark.parametrize("payload", ['{"has_more":"false"}', '{"has_more":0}', "[]", "null"])
def test_malformed_checkpoint_is_rejected(payload: str) -> None:
    with pytest.raises(ValueError):
        module.SlackConnector().validate_checkpoint_json(payload)


@pytest.mark.parametrize("config", [{"channels": ""}, {"channels": [], "batch_size": False}, {"channels": [], "batch_size": 0}, {"channels": [], "channel_regex_enabled": "false"}])
async def test_worker_invalid_config_fails_before_provider_io(config: dict[str, Any]) -> None:
    from core.svr.sync_data_source import Slack

    driver = Slack({**config, "credentials": {"slack_bot_token": "controlled-token"}})
    with pytest.raises((ValueError, module.ConnectorValidationError)):
        await driver._generate({"id": "unit", "tenant_id": "tenant", "timeout_secs": 30, "reindex": "0", "poll_range_start": None})


def test_missing_credentials_and_empty_checkpoint() -> None:
    connector = module.SlackConnector()
    with pytest.raises(module.ConnectorMissingCredentialError):
        list(connector.load_from_state())
    with pytest.raises(NotImplementedError):
        connector.load_credentials({})
    with pytest.raises(module.ConnectorMissingCredentialError):
        connector.set_credentials_provider(StaticCredentialsProvider("tenant", "slack", {}))
    checkpoint = connector.validate_checkpoint_json('{"has_more":false}')
    generator = connector.load_from_checkpoint(0.0, 1.0, checkpoint)
    with pytest.raises(StopIteration) as stop:
        next(generator)
    assert stop.value.value.has_more is False


@pytest.mark.parametrize("mode", ["mention-scope", "mention-ratelimit"])
def test_cached_sender_does_not_hide_mention_lookup_errors(slack: tuple[Any, Any], mode: str) -> None:
    from common.data_source.models import BasicExpertInfo

    connector, http = slack
    connector.user_cache["U1"] = BasicExpertInfo(display_name="Cached Alice")
    http.mode = mode
    with pytest.raises(SlackApiError):
        list(connector.load_from_state())
    assert [call["params"]["user"] for call in http.calls if call["method"] == "users.info"] == ["UBAD"]
