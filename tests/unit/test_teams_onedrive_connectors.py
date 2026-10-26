from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from common.data_source.exceptions import ConnectorValidationError
from common.data_source.interfaces import CheckpointOutputWrapper
from common.data_source.microsoft_graph import GraphConnection
from common.data_source.onedrive_connector import OneDriveConnector
from common.data_source.teams_connector import TeamsConnector
from tests.support.resource_journal import ResourceJournal
from tests.support.teams_onedrive_fakes import CREDS, MODIFIED, OLD, MicrosoftHTTP, install_graph


@pytest.fixture(autouse=True)
def journal(request: pytest.FixtureRequest) -> Iterator[None]:
    record = ResourceJournal(request.node.nodeid)
    yield
    record.event("case-complete", external_resources=[])


@pytest.fixture(params=["teams", "onedrive"])
def graph(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MicrosoftHTTP, dict[str, Any]]:
    http = MicrosoftHTTP(request.param)
    auth = install_graph(monkeypatch, http)
    adapter = TeamsConnector(batch_size=1) if request.param == "teams" else OneDriveConnector(batch_size=1, drive_ids=["A", "B"])
    adapter.load_credentials(CREDS)
    return adapter, http, auth


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_real_sdk_paging_lazy_auth_and_content(graph: tuple[Any, MicrosoftHTTP, dict[str, Any]]) -> None:
    adapter, http, auth = graph
    assert auth["constructed"] == 0 and not http.calls
    docs = [doc for batch in adapter.poll_source(epoch(OLD), epoch(MODIFIED)) for doc in batch]
    assert auth["constructed"] == 1
    if http.source == "teams":
        assert {doc.id for doc in docs} == {"t1__c1__m1", "t1__c1__m2"}
        assert docs[0].extension == ".html"
        assert b"<b>old parent</b>" in docs[0].blob and b"new reply &lt;script&gt;" in docs[0].blob
        assert b"https://contoso.sharepoint.com/report.txt" in docs[0].blob
        assert not any("sharepoint.com" in url for url in http.calls)
    else:
        assert {doc.id for doc in docs} == {"A:same", "A:nested", "B:same"}
        assert all(doc.blob == http.body and doc.size_bytes == len(http.body) for doc in docs)
    assert all(doc.doc_updated_at.timestamp() == epoch(MODIFIED) for doc in docs)


def test_window_exclusive_lower_and_finished_checkpoint(graph: tuple[Any, MicrosoftHTTP, dict[str, Any]]) -> None:
    adapter, http, _ = graph
    assert list(adapter.poll_source(epoch(MODIFIED), epoch(MODIFIED))) == []
    ck = adapter.build_dummy_checkpoint()
    output = list(CheckpointOutputWrapper()(adapter.load_from_checkpoint(0.0, epoch(MODIFIED), ck)))
    assert output[-1][2].has_more is False and ck.has_more is True
    finished = output[-1][2]
    assert list(adapter.load_from_checkpoint(0.0, epoch(MODIFIED), finished)) == []
    assert adapter.validate_checkpoint_json(finished.model_dump_json()).has_more is False
    with pytest.raises(ValueError):
        adapter.validate_checkpoint_json("broken")
    if http.source == "onedrive":
        assert ck.delta_links == {} and set(finished.delta_links) == {"A", "B"}


@pytest.mark.parametrize("mode", ["permission", "later-page", "content", "malformed", "repeat", "untrusted"])
def test_failures_propagate_without_finishing_checkpoint(graph: tuple[Any, MicrosoftHTTP, dict[str, Any]], mode: str) -> None:
    adapter, http, _ = graph
    http.mode = mode
    ck = adapter.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError) as exc:
        list(adapter.load_from_checkpoint(0.0, epoch(MODIFIED), ck))
    assert ck.has_more and "controlled-secret-must-not-leak" not in str(exc.value)


def test_slim_identity_and_no_download(graph: tuple[Any, MicrosoftHTTP, dict[str, Any]]) -> None:
    adapter, http, _ = graph
    slim = [doc.id for batch in adapter.retrieve_all_slim_docs_perm_sync() for doc in batch]
    assert len(slim) == (2 if http.source == "teams" else 3)
    assert not any(url.endswith("/content") for url in http.calls)


@pytest.mark.parametrize("source", ["teams", "onedrive"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_final_response_stop_prevents_documents(monkeypatch: pytest.MonkeyPatch, source: str, stop: str) -> None:
    http = MicrosoftHTTP(source)
    install_graph(monkeypatch, http)
    stopped = False

    def check() -> None:
        if stopped:
            raise InterruptedError(stop)

    def after(path: str) -> None:
        nonlocal stopped
        if path.endswith("/content") or path.endswith("/replies"):
            stopped = True

    http.after_request = after
    adapter = TeamsConnector(check_active=check) if source == "teams" else OneDriveConnector(drive_ids=["A"], check_active=check)
    adapter.load_credentials(CREDS)
    with pytest.raises(InterruptedError):
        list(adapter.poll_source(0.0, epoch(MODIFIED)))


@pytest.mark.parametrize("drives", [None, "A", [""], ["A", "A"], [True]])
def test_onedrive_requires_explicit_scope(drives: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        OneDriveConnector(drive_ids=drives)


def test_explicit_empty_scope_never_becomes_tenant_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    http = MicrosoftHTTP("onedrive")
    auth = install_graph(monkeypatch, http)
    adapter = OneDriveConnector(drive_ids=[])
    adapter.load_credentials(CREDS)
    adapter.validate_connector_settings()
    assert list(adapter.poll_source(0.0, epoch(MODIFIED))) == []
    assert list(adapter.retrieve_all_slim_docs_perm_sync()) == []
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.parametrize("path", ["../secret", "Documents/../secret", ".", 1])
def test_folder_path_rejects_traversal(path: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        OneDriveConnector(drive_ids=["A"], folder_path=path)


def test_folder_encoding_and_missing_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    http = MicrosoftHTTP("onedrive")
    install_graph(monkeypatch, http)
    adapter = OneDriveConnector(drive_ids=["A"], folder_path=" Documents/a#b?c% ")
    adapter.load_credentials(CREDS)
    assert adapter._delta_url("A").endswith("/items/scoped-folder/delta")
    assert "/root:/Documents/a%23b%3Fc%25" in http.calls[-1]
    http.mode = "missing-delta"
    with pytest.raises(ConnectorValidationError, match="Incomplete"):
        list(adapter.poll_source(0.0, epoch(MODIFIED)))


@pytest.mark.parametrize("url", ["https://untrusted.test/v1.0/next", "http://graph.microsoft.com/v1.0/next", "https://graph.microsoft.com@untrusted.test/v1.0/next"])
def test_untrusted_url_rejected_before_auth(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    http = MicrosoftHTTP("onedrive")
    auth = install_graph(monkeypatch, http)
    with pytest.raises(ConnectorValidationError):
        GraphConnection(CREDS, lambda: None).read(url)
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.parametrize("source", ["teams", "onedrive"])
@pytest.mark.parametrize("value", [None, "broken", "2026-01-02T00:00:00", 0])
def test_bad_source_time_fails_closed(monkeypatch: pytest.MonkeyPatch, source: str, value: Any) -> None:
    http = MicrosoftHTTP(source)
    install_graph(monkeypatch, http)
    if source == "onedrive":
        original = http.file
        monkeypatch.setattr(http, "file", lambda identifier, name: {**original(identifier, name), "lastModifiedDateTime": value})
        adapter: Any = OneDriveConnector(drive_ids=["A"])
    else:
        original = http.message
        monkeypatch.setattr(http, "message", lambda *args, **kwargs: {**original(*args, **kwargs), "lastModifiedDateTime": value})
        adapter = TeamsConnector()
    adapter.load_credentials(CREDS)
    with pytest.raises(ConnectorValidationError):
        list(adapter.poll_source(0.0, epoch(MODIFIED)))


def test_delta_checkpoint_resumes_without_time_dropping_restored_files(monkeypatch: pytest.MonkeyPatch) -> None:
    http = MicrosoftHTTP("onedrive")
    install_graph(monkeypatch, http)
    adapter = OneDriveConnector(drive_ids=["A"])
    adapter.load_credentials(CREDS)
    checkpoint = adapter.build_dummy_checkpoint()
    checkpoint.delta_links = {"A": "https://graph.microsoft.com/v1.0/drives/A/root/delta?token=saved"}
    result = list(CheckpointOutputWrapper()(adapter.load_from_checkpoint(epoch(MODIFIED), epoch(MODIFIED), checkpoint)))
    assert sum(document is not None for document, _, _ in result) == 2
    assert "token=saved" in http.calls[0]
    assert checkpoint.has_more


@pytest.mark.parametrize("body", [b"", b'{"zero":0,"empty":[]}'])
def test_actual_zero_and_json_file_bytes(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    http = MicrosoftHTTP("onedrive")
    http.body = body
    install_graph(monkeypatch, http)
    adapter = OneDriveConnector(drive_ids=["A"])
    adapter.load_credentials(CREDS)
    docs = [doc for batch in adapter.poll_source(0.0, epoch(MODIFIED)) for doc in batch]
    assert len(docs) == 2 and all(doc.blob == body and doc.size_bytes == len(body) for doc in docs)


@pytest.mark.parametrize("mode", ["redirect-external", "redirect-signed", "size", "transport"])
def test_download_bearer_size_and_transport_boundaries(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    import requests

    from common.data_source import microsoft_graph as module

    calls: list[tuple[str, dict[str, str]]] = []

    def network(url: str, **kwargs: Any) -> requests.Response:
        calls.append((url, kwargs["headers"]))
        if mode == "transport":
            raise requests.ReadTimeout("sensitive raw response")
        response = requests.Response()
        response.url = url
        response.status_code = 302 if len(calls) == 1 and mode.startswith("redirect") else 200
        response.headers["Location"] = "https://cdn.contoso.sharepoint.com/signed" if mode == "redirect-signed" else "https://attacker.test/blob"
        response._content, response._content_consumed = b"bytes", True
        return response

    monkeypatch.setattr(module, "_send_request", lambda session, method, url, **kwargs: network(url, **kwargs))
    if mode == "size":
        monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 3)
    connection = GraphConnection(CREDS, lambda: None)
    if mode == "redirect-signed":
        assert connection.read("https://graph.microsoft.com/v1.0/drives/A/items/x/content", headers={"Authorization": "Bearer controlled-token"}, content=True).content == b"bytes"
        assert "Authorization" not in calls[1][1]
    else:
        with pytest.raises(ConnectorValidationError) as exc:
            connection.read("https://graph.microsoft.com/v1.0/drives/A/items/x/content", headers={"Authorization": "Bearer controlled-token"}, content=True)
        assert len(calls) == 1 and "sensitive raw response" not in str(exc.value)


def test_folder_scoped_cursor_cannot_switch_to_root_or_another_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    from common.data_source.onedrive_connector import OneDriveCheckpoint

    http = MicrosoftHTTP("onedrive")
    install_graph(monkeypatch, http)
    adapter = OneDriveConnector(drive_ids=["A"], folder_path="Documents")
    adapter.load_credentials(CREDS)
    for path in ("root", "items/another-folder"):
        checkpoint = OneDriveCheckpoint(scope=adapter.scope, delta_links={"A": f"https://graph.microsoft.com/v1.0/drives/A/{path}/delta?token=bad"})
        with pytest.raises(ConnectorValidationError, match="folder scope"):
            adapter.validate_checkpoint_json(checkpoint.model_dump_json())
        with pytest.raises(ConnectorValidationError, match="folder scope"):
            list(adapter.load_from_checkpoint(0.0, epoch(MODIFIED), checkpoint))
    assert not any("/delta" in url or "/content" in url for url in http.calls)
    docs = [doc for batch in adapter.poll_source(0.0, epoch(MODIFIED)) for doc in batch]
    assert len(docs) == 2
    assert all("/items/scoped-folder/delta" in url for url in http.calls if "/delta" in url)


@pytest.mark.parametrize("source", ["teams", "onedrive"])
@pytest.mark.parametrize("field,value", [(field, value) for field in ("batch_size", "sync_batch_size") for value in (0, False, "bad")])
async def test_public_probe_rejects_bad_batch_before_auth(monkeypatch: pytest.MonkeyPatch, source: str, field: str, value: Any) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import AsyncSession

    from api.apps.services import connector_api_service

    http = MicrosoftHTTP(source)
    auth = install_graph(monkeypatch, http)
    config = {"credentials": CREDS, "drive_ids": ["A"], field: value}
    db = AsyncSession()
    monkeypatch.setattr(db, "scalar", AsyncMock(return_value=SimpleNamespace(source=source, config=config)))
    try:
        success, _data, code = await connector_api_service.test_connector(db, "controlled-connector")
        assert not success and code is not None
        assert not http.calls and auth["constructed"] == 0
    finally:
        await db.close()


async def test_public_empty_scope_is_not_fake_credential_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import AsyncSession

    from api.apps.services import connector_api_service

    http = MicrosoftHTTP("onedrive")
    auth = install_graph(monkeypatch, http)
    db = AsyncSession()
    monkeypatch.setattr(db, "scalar", AsyncMock(return_value=SimpleNamespace(source="onedrive", config={"credentials": CREDS, "drive_ids": []})))
    try:
        success, message, code = await connector_api_service.test_connector(db, "controlled-connector")
        assert not success and "at least one configured drive" in message and code is not None
        assert auth["constructed"] == 0 and not http.calls and not db.new and not db.dirty
    finally:
        await db.close()
