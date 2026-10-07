import email
from datetime import UTC, datetime
from typing import Any

import pytest

from common.data_source import imap_connector as imap
from common.data_source.interfaces import CheckpointOutputWrapper, collect_slim_document_snapshot
from common.data_source.models import Document
from core.svr import sync_data_source as sync_module
from tests.support.imap_source import ImapSource, message
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def connector(monkeypatch: pytest.MonkeyPatch, source: ImapSource, **kwargs: Any) -> imap.ImapConnector:
    result = imap.ImapConnector("imap.test", **kwargs)
    monkeypatch.setattr(result, "_get_mail_client", lambda: source)
    return result


def load(result: imap.ImapConnector, start: float, end: float) -> list[Document]:
    documents = []
    checkpoint = result.build_dummy_checkpoint()
    while checkpoint.has_more:
        wrapper = CheckpointOutputWrapper()
        for document, failure, next_checkpoint in wrapper(result.load_from_checkpoint(start, end, checkpoint)):
            assert failure is None
            if document is not None:
                documents.append(document)
            if next_checkpoint is not None:
                checkpoint = next_checkpoint
    return documents


@pytest.mark.parametrize("missing_id", [False, True])
def test_content_and_snapshot_share_mail_attachment_and_stable_generated_ids(monkeypatch: pytest.MonkeyPatch, missing_id: bool) -> None:
    raw = message(None if missing_id else "<retained@test>")
    source = ImapSource(messages={"1": raw})
    result = connector(monkeypatch, source)
    content = load(result, 0, datetime(2026, 12, 1, tzinfo=UTC).timestamp())
    assert len(content) == 2 and content[1].semantic_identifier == "报告.txt"
    ids = [document.id for document in content]
    assert ids == [document.id for document in collect_slim_document_snapshot(result)]
    assert ids == [document.id for document in load(result, 0, datetime(2026, 12, 1, tzinfo=UTC).timestamp())]
    if missing_id:
        assert ids[0].startswith("generated:")
    else:
        assert ids[0] == "<retained@test>"
    assert source.logged_out >= 5
    assert any(call == ("SELECT", '"Archive"', True) for call in source.calls)


@pytest.mark.parametrize("mode", ["list-error", "malformed", "select-error", "search-error", "bad-search", "second-mailbox-error", "read-error", "missing-read", "wrong-uid", "later-read-error"])
def test_partial_enumeration_and_read_failures_never_publish(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    source = ImapSource(mode, {"1": message(), "2": message("<second@test>")})
    monkeypatch.setattr(imap, "_PAGE_SIZE", 1)
    with pytest.raises(RuntimeError):
        collect_slim_document_snapshot(connector(monkeypatch, source))
    assert source.logged_out == 1


@pytest.mark.parametrize("mode", ["empty", "empty-mailboxes"])
def test_successful_empty_inventory_is_authoritative(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    assert collect_slim_document_snapshot(connector(monkeypatch, ImapSource(mode))) == ()


def test_exact_utc_open_start_closed_end_not_internaldate(monkeypatch: pytest.MonkeyPatch) -> None:
    start, end = datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 1, 2, tzinfo=UTC).timestamp()
    raws = {"1": message("start", date="Thu, 01 Jan 2026 08:00:00 +0800"), "2": message("end", date="Fri, 02 Jan 2026 08:00:00 +0800"), "3": message("after", date="Fri, 02 Jan 2026 08:00:01 +0800")}
    result = connector(monkeypatch, ImapSource(messages=raws), snapshot_start=start, snapshot_end=end)
    assert [doc.id for doc in load(result, start, end)] == ["end", "end#att:报告.txt"]
    assert [doc.id for doc in collect_slim_document_snapshot(result)] == ["end", "end#att:报告.txt"]


def test_missing_date_uses_stable_internaldate_and_id_is_independent_of_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = message(None, date=None)
    first = imap.EmailHeaders.from_email_msg(email.message_from_bytes(raw))
    second = imap.EmailHeaders.from_email_msg(email.message_from_bytes(raw))
    assert first.id == second.id
    result = connector(monkeypatch, ImapSource(messages={"1": raw}))
    docs = load(result, 0, datetime(2026, 12, 1, tzinfo=UTC).timestamp())
    assert docs[0].id == first.id and docs[0].doc_updated_at == datetime(2026, 10, 7, 4, tzinfo=UTC)
    assert [doc.id for doc in collect_slim_document_snapshot(result)] == [doc.id for doc in docs]


def test_filename_charset_fallback_and_attachment_indexing_eligibility() -> None:
    assert imap.decode_mime_filename("=?unknown-codec?b?5oql5ZGKLnR4dA==?=") == "报告.txt"
    raw = message()
    parsed = email.message_from_bytes(raw)
    assert len(imap.extract_attachments(parsed)) == 1
    assert imap.extract_attachments(parsed, max_bytes=1) == []


@pytest.mark.parametrize("mailboxes", ["INBOX", [""], [None]])
def test_mailbox_configuration_is_validated(mailboxes: Any) -> None:
    with pytest.raises(ValueError):
        imap.ImapConnector("host", mailboxes=mailboxes)


@pytest.mark.parametrize("mode", ["complete", "empty", "read-error", "disabled", "first", "reindex", "persistence-error"])
async def test_driver_scope_and_snapshot_coordinator(sync_env: dict[str, Any], mode: str) -> None:
    source = ImapSource(mode)
    scopes = []

    def persist(*args: Any, **kwargs: Any) -> float:
        scopes.append(kwargs)
        if mode == "persistence-error":
            raise RuntimeError("scope not saved")
        return 0

    patch = sync_env["monkeypatch"]
    patch.setattr(sync_module.ConnectorService, "ensure_imap_sync_scope", persist)
    patch.setattr(imap.ImapConnector, "_get_mail_client", lambda self: source)
    patch.setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda *args: ([], []))
    conf = {
        "imap_host": "imap.test",
        "imap_port": 993,
        "imap_mailbox": [],
        "poll_range": "30",
        "credentials": {"imap_username": "owner", "imap_password": "synthetic"},
        "sync_deleted_files": mode != "disabled",
    }
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await sync_module.IMAP(conf)(current)
    calls = sync_env["calls"]
    assert calls[0] == "start" and calls[-1] == ("fail" if mode in {"read-error", "persistence-error"} else "complete")
    assert any(isinstance(call, tuple) and call[0] == "cleanup" for call in calls) == (mode in {"complete", "empty"})
    assert scopes == [{"reindex": mode == "reindex", "incremental": mode != "first"}]
    if mode == "persistence-error":
        assert source.calls == []


@pytest.mark.parametrize("raised", [False, True])
def test_failed_login_closes_connection(monkeypatch: pytest.MonkeyPatch, raised: bool) -> None:
    class FailedLogin:
        closed = False

        def login(self, **kwargs: Any) -> tuple[str, list[bytes]]:
            if raised:
                raise RuntimeError("login rejected")
            return "NO", []

        def shutdown(self) -> None:
            self.closed = True

    client = FailedLogin()
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", lambda **kwargs: client)
    result = imap.ImapConnector("imap.test")
    result._credentials = {"imap_username": "user", "imap_password": "secret"}
    with pytest.raises(RuntimeError):
        result._get_mail_client()
    assert client.closed
