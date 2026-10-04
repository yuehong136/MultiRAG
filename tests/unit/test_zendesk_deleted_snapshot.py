from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source import zendesk_connector as module
from common.data_source.interfaces import CheckpointOutputWrapper, collect_slim_document_snapshot
from common.data_source.models import Document
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def article(identity: int = 1, **changes: Any) -> dict[str, Any]:
    return {"id": identity, "title": "Article", "body": "<p>body</p>", "draft": False, "label_names": [], "updated_at": "2026-02-01T00:00:00Z", **changes}


def connector(records: list[dict[str, Any]], content_type: str = "articles", mode: str = "") -> module.ZendeskConnector:
    result = module.ZendeskConnector(content_type)

    def request(endpoint: str, params: Any) -> dict[str, Any]:
        if endpoint == "guide/content_tags":
            return {"records": [], "meta": {"has_more": False}}
        if endpoint == "help_center/articles":
            if params.get("page[after]"):
                if mode == "page-error":
                    raise PermissionError("denied")
                if mode == "malformed":
                    return {"articles": []}
                return {"articles": [], "meta": {"has_more": mode == "loop", "after_cursor": "next"}}
            return {"articles": records, "meta": {"has_more": bool(mode), "after_cursor": "next"}}
        if endpoint == "incremental/tickets.json":
            return {"tickets": records, "end_of_stream": True, "end_time": 1770000000}
        if endpoint.endswith("/comments"):
            return {"comments": [], "meta": {"has_more": False}}
        raise AssertionError(endpoint)

    result.client = SimpleNamespace(make_request=request)
    return result


def body_docs(result: module.ZendeskConnector) -> list[Document]:
    checkpoint = result.build_dummy_checkpoint()
    documents = []
    while checkpoint.has_more:
        for document, failure, next_checkpoint in CheckpointOutputWrapper()(result.load_from_checkpoint(0, datetime(2026, 3, 1, tzinfo=UTC).timestamp(), checkpoint)):
            assert failure is None
            if document is not None:
                documents.append(document)
            if next_checkpoint is not None:
                checkpoint = next_checkpoint
    return documents


@pytest.mark.parametrize("body", [None, "", "  ", "<p> </p>"])
def test_empty_articles_excluded_from_both_paths(body: Any) -> None:
    result = connector([article(body=body)])
    assert collect_slim_document_snapshot(result) == ()
    assert body_docs(result) == []


def test_article_eligibility_and_ids_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "ZENDESK_CONNECTOR_SKIP_ARTICLE_LABELS", ["exclude"])
    result = connector([article(1), article(2, draft=True), article(3, label_names=["exclude"]), article(4, label_names=None)])
    assert [d.id for d in collect_slim_document_snapshot(result)] == [d.id for d in body_docs(result)] == ["article:1", "article:4"]


@pytest.mark.parametrize("mode", ["page-error", "malformed", "loop"])
def test_partial_and_looping_article_pages_fail_both_paths(mode: str) -> None:
    result = connector([article()], mode=mode)
    with pytest.raises((ValueError, PermissionError)):
        collect_slim_document_snapshot(result)
    with pytest.raises((ValueError, PermissionError)):
        body_docs(result)


@pytest.mark.parametrize("record", [{"id": 1}, article(id=None), article(draft="false"), article(label_names="label"), article(label_names=False), article(label_names=[1])])
def test_unknown_eligibility_is_not_an_empty_snapshot(record: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        collect_slim_document_snapshot(connector([record]))


def test_ticket_body_excludes_deleted_but_export_is_not_a_deletion_snapshot() -> None:
    result = connector([{"id": 1, "status": "open", "subject": "Live", "updated_at": "2026-02-01T00:00:00Z"}, {"id": 2, "status": "deleted"}], "tickets")
    assert [doc.id for doc in body_docs(result)] == ["zendesk_ticket_1"]
    with pytest.raises(module.ConnectorValidationError, match="most recent minute"):
        collect_slim_document_snapshot(result)


@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "content-failure", "tickets"])
async def test_real_driver_gate_and_failure(sync_env: dict[str, Any], mode: str) -> None:
    patch = sync_env["monkeypatch"]
    result = connector([article()], content_type="tickets" if mode == "tickets" else "articles")
    patch.setattr(result, "load_credentials", lambda _: None)
    patch.setattr(sync_module, "ZendeskConnector", lambda **kw: result)
    patch.setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda db, kb, docs, *args: ([], [doc["id"] for doc in docs]))
    if mode == "content-failure":
        patch.setattr(module, "_article_to_document", lambda *args: (_ for _ in ()).throw(ValueError("body")))
    driver = sync_module.Zendesk({"sync_deleted_files": mode != "disabled", "credentials": {}})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    if mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    if mode in {"content-failure", "tickets"}:
        assert sync_env["calls"] == ["start", "fail"]
    else:
        assert sync_env["calls"][-1] == "complete"
        assert any(isinstance(call, tuple) and call[0] == "cleanup" for call in sync_env["calls"]) == (mode == "incremental")
