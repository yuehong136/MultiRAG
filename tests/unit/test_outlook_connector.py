"""Outlook scope, delta, payload, lazy auth and bounded Graph contracts."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointOutputWrapper
from common.data_source.models import ConnectorCheckpoint
from common.data_source.outlook_connector import OutlookConnector
from tests.support.outlook_fakes import OutlookHTTP
from tests.support.resource_journal import ResourceJournal
from tests.support.teams_onedrive_fakes import CREDS, MODIFIED, install_graph


@pytest.fixture(autouse=True)
def journal(request: pytest.FixtureRequest) -> Iterator[None]:
    record = ResourceJournal(request.node.nodeid)
    yield
    record.event("case-complete", external_resources=[])


def epoch(value: str = MODIFIED) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]:
    http = OutlookHTTP()
    auth = install_graph(monkeypatch, http)
    adapter = OutlookConnector(batch_size=1, user_ids=["A", "B"])
    adapter.load_credentials(CREDS)
    return adapter, http, auth


def test_pagination_latest_states_compound_identity_and_content(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, auth = graph
    assert auth["constructed"] == 0 and not http.calls
    docs = [doc for batch in adapter.poll_source(0.0, epoch()) for doc in batch]
    assert auth["constructed"] == 1 and len(http.calls) == 4
    assert {doc.id for doc in docs} == {"A:same", "A:second", "B:same", "B:second"}
    assert all(doc.semantic_identifier == "Latest <subject>" and doc.extension == ".html" for doc in docs)
    assert all(doc.doc_updated_at.timestamp() == epoch() and len(doc.blob) == doc.size_bytes for doc in docs)
    assert all(b"<p>Actual mail body &amp; bytes</p>" in doc.blob and b"&lt;Sender&gt;" in doc.blob and b"Latest &lt;subject&gt;" in doc.blob for doc in docs)
    assert all(doc.metadata["has_attachments"] is True and doc.externale_access is None for doc in docs)
    assert not any("attachments" in url for url in http.calls)


def test_checkpoint_is_returned_not_mutated_and_delta_does_not_drop_old_updates(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, _ = graph
    ck = adapter.build_dummy_checkpoint()
    output = list(CheckpointOutputWrapper()(adapter.load_from_checkpoint(0.0, epoch(), ck)))
    done = output[-1][2]
    assert ck.has_more and ck.delta_links == {} and not done.has_more
    assert adapter.validate_checkpoint_json(done.model_dump_json()) == done
    count = len(http.calls)
    assert list(adapter.load_from_checkpoint(0.0, epoch(), done)) == []
    assert len(http.calls) == count
    resumed = done.model_copy(update={"has_more": True})
    results = list(CheckpointOutputWrapper()(adapter.load_from_checkpoint(epoch(), epoch(), resumed)))
    assert sum(doc is not None for doc, _, _ in results) == 4
    assert "token=saved" in http.calls[count]
    assert list(adapter.poll_source(epoch(), epoch())) == []


@pytest.mark.parametrize(
    "mode",
    ["permission", "unauthorized", "not-found", "server-error", "throttled", "later-page", "repeat", "missing-delta", "ambiguous", "cross-mailbox", "cross-folder", "malformed", "bad-id", "json"],
)
def test_failed_enumeration_does_not_finish_checkpoint(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]], mode: str) -> None:
    adapter, http, _ = graph
    http.mode = mode
    ck = adapter.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError) as exc:
        list(adapter.load_from_checkpoint(0.0, epoch(), ck))
    assert ck.has_more and ck.delta_links == {}
    assert "controlled-secret-must-not-leak" not in str(exc.value)


@pytest.mark.parametrize("users", [None, "A,B", [""], [" A"], ["A", "A"], [True], [1], [".."], ["A\nB"], {}])
def test_scope_is_explicit_strict_array(users: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        OutlookConnector(user_ids=users)


@pytest.mark.parametrize("field,value", [("batch_size", True), ("batch_size", "1"), ("batch_size", 0), ("batch_size", None), ("folder", None), ("folder", ""), ("folder", ".."), ("folder", " inbox")])
def test_config_is_strict(field: str, value: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        OutlookConnector(user_ids=["A"], **{field: value})


def test_empty_scope_no_auth_no_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    http = OutlookHTTP()
    auth = install_graph(monkeypatch, http)
    adapter = OutlookConnector(user_ids=[])
    adapter.load_credentials(CREDS)
    adapter.validate_connector_settings()
    assert list(adapter.poll_source(0.0, epoch())) == []
    assert not http.calls and auth["constructed"] == 0


def test_probe_checks_every_mailbox_messages(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, _ = graph
    adapter.validate_connector_settings()
    assert len(http.calls) == 2 and all("/messages?$top=1&$select=id" in url for url in http.calls)


@pytest.mark.parametrize("credentials", [None, {}, {"tenant_id": "a/b", "client_id": "c", "client_secret": "s"}, {**CREDS, "client_secret": " "}])
def test_invalid_credentials_fail_before_network(credentials: Any) -> None:
    with pytest.raises((ConnectorMissingCredentialError, ConnectorValidationError)):
        OutlookConnector(user_ids=["A"]).load_credentials(credentials)


def test_token_failure_redacted(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, auth = graph
    auth["result"] = {"error_description": "controlled-secret-must-not-leak"}
    with pytest.raises(ConnectorMissingCredentialError) as exc:
        adapter.validate_connector_settings()
    assert "controlled-secret-must-not-leak" not in str(exc.value) and not http.calls


@pytest.mark.parametrize(
    "link",
    [
        "https://attacker.test/v1.0/x",
        "https://graph.microsoft.com/v1.0/users/B/mailFolders/inbox/messages/delta?x=1",
        "https://graph.microsoft.com/v1.0/users/A/mailFolders/archive/messages/delta?x=1",
        "https://graph.microsoft.com/v1.0/users/A/messages/delta?x=1",
    ],
)
def test_cursor_scope_rejected_before_auth(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]], link: str) -> None:
    adapter, http, auth = graph
    ck = adapter.build_dummy_checkpoint()
    ck.delta_links = {"A": link}
    with pytest.raises(ConnectorValidationError):
        list(adapter.load_from_checkpoint(0.0, epoch(), ck))
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.parametrize("raw", ["garbage", "{}", '{"has_more":"true"}', '{"has_more":true,"scope":"wrong"}'])
def test_bad_checkpoint_never_resets(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]], raw: str) -> None:
    with pytest.raises((ValueError, ConnectorValidationError)):
        graph[0].validate_checkpoint_json(raw)


def test_checkpoint_type_or_scope_mismatch(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, auth = graph
    for ck in (ConnectorCheckpoint(has_more=True), OutlookConnector(user_ids=["A"], folder="archive").build_dummy_checkpoint()):
        with pytest.raises(ConnectorValidationError):
            list(adapter.load_from_checkpoint(0.0, epoch(), ck))
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.parametrize("start,end", [(-1.0, 1.0), (2.0, 1.0), (float("nan"), 1.0), (0.0, float("inf")), (True, 1.0)])
def test_invalid_window(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]], start: float, end: float) -> None:
    adapter, http, auth = graph
    with pytest.raises(ConnectorValidationError):
        list(adapter.poll_source(start, end))
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("body", None),
        ("body", {"contentType": "html", "content": None}),
        ("body", {"contentType": "unknown", "content": "x"}),
        ("toRecipients", None),
        ("ccRecipients", [{}]),
        ("from", []),
        ("subject", 1),
        ("hasAttachments", "false"),
        ("webLink", None),
        ("lastModifiedDateTime", None),
        ("lastModifiedDateTime", "2026-01-02T00:00:00"),
        ("receivedDateTime", "invalid"),
        ("lastModifiedDateTime", "2027-01-01T00:00:00Z"),
    ],
)
def test_invalid_message_fails_closed(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]], field: str, value: Any) -> None:
    adapter, http, _ = graph
    original = http.message
    http.message = lambda *args: {**original(*args), field: value}
    with pytest.raises(ConnectorValidationError):
        list(adapter.poll_source(0.0, epoch()))


def test_text_body_empty_subject_and_safe_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    http = OutlookHTTP()
    install_graph(monkeypatch, http)
    original = http.message
    http.message = lambda *args: {**original(*args), "id": "id:with/slash", "subject": None, "body": {"contentType": "text", "content": "<literal>&"}}
    adapter = OutlookConnector(user_ids=["alice+one@example.test"], folder="folder/#?%")
    adapter.load_credentials(CREDS)
    docs = [doc for batch in adapter.poll_source(0.0, epoch()) for doc in batch]
    assert len(docs) == 1
    assert docs[0].id == "alice%2Bone%40example.test:id%3Awith%2Fslash"
    assert b"<pre>&lt;literal&gt;&amp;</pre>" in docs[0].blob
    assert all("folder%2F%23%3F%25" in url for url in http.calls)


def test_stop_after_last_response_prevents_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    http = OutlookHTTP()
    install_graph(monkeypatch, http)
    stopped = False

    def check() -> None:
        if stopped:
            raise InterruptedError("cancelled")

    def after(path: str) -> None:
        nonlocal stopped
        if len(http.calls) == 2:
            stopped = True

    http.after_request = after
    adapter = OutlookConnector(user_ids=["A"], check_active=check)
    adapter.load_credentials(CREDS)
    with pytest.raises(InterruptedError):
        list(adapter.poll_source(0.0, epoch()))


def test_deletion_snapshot_is_unavailable(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    with pytest.raises(ConnectorValidationError, match="not supported"):
        list(graph[0].retrieve_all_slim_docs_perm_sync())
    assert not graph[1].calls


def test_finished_checkpoint_requires_every_mailbox(graph: tuple[OutlookConnector, OutlookHTTP, dict[str, Any]]) -> None:
    adapter, http, auth = graph
    checkpoint = adapter.build_dummy_checkpoint()
    checkpoint.has_more = False
    checkpoint.delta_links = {"A": adapter._delta_url("A") + "?token=saved"}
    with pytest.raises(ConnectorValidationError, match="Incomplete"):
        adapter.validate_checkpoint_json(checkpoint.model_dump_json())
    assert not http.calls and auth["constructed"] == 0
