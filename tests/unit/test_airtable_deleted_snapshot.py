from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.airtable_connector import AirtableConnector
from common.data_source.interfaces import collect_slim_document_snapshot


def connector(monkeypatch: pytest.MonkeyPatch, pages: Iterator[list[dict[str, Any]]]) -> AirtableConnector:
    value = AirtableConnector("base", "table", batch_size=1)

    def raw_pages(**kwargs: Any) -> Iterator[dict[str, Any]]:
        for records in pages:
            yield {"records": records}

    table = SimpleNamespace(api=SimpleNamespace(iterate_requests=raw_pages), urls=SimpleNamespace(records="https://example.test/records", records_post="https://example.test/listRecords"))
    value._airtable_client = SimpleNamespace(table=lambda *args: table)  # type: ignore[assignment]
    monkeypatch.setattr("common.data_source.airtable_connector.requests.get", lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, content=b"file"))
    return value


def record() -> dict[str, Any]:
    return {
        "id": "rec1",
        "createdTime": "2026-01-01T00:00:00.000Z",
        "fields": {"tags": ["one", "two"], "collaborators": [{"id": "usr1"}], "attachments": [{"id": "att1", "filename": "a.txt", "url": "https://example.test/a", "size": 4}]},
    }


def test_attachment_snapshot_matches_ingestion_and_does_not_download(monkeypatch: pytest.MonkeyPatch) -> None:
    value = connector(monkeypatch, iter([[record()]]))
    imported = [doc.id for batch in value.load_from_state() for doc in batch]
    value = connector(monkeypatch, iter([[record()]]))
    monkeypatch.setattr("common.data_source.airtable_connector.requests.get", lambda *args, **kwargs: pytest.fail("snapshot downloaded content"))
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == imported == ["airtable:rec1:att1"]


def test_snapshot_preserves_oversized_and_temporarily_unavailable_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    item = record()
    item["fields"]["attachments"][0].pop("url")
    value = connector(monkeypatch, iter([[item]]))
    value.size_threshold = 1
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == ["airtable:rec1:att1"]


def test_later_page_denied_cannot_publish_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def pages() -> Iterator[list[dict[str, Any]]]:
        yield [record()]
        raise PermissionError("later page denied")

    with pytest.raises(PermissionError):
        collect_slim_document_snapshot(connector(monkeypatch, pages()))
    assert collect_slim_document_snapshot(connector(monkeypatch, iter([[]]))) == ()


@pytest.mark.parametrize("identifier", [None, "", "  "])
def test_incomplete_attachment_aborts_instead_of_becoming_empty(monkeypatch: pytest.MonkeyPatch, identifier: str | None) -> None:
    item = record()
    item["fields"]["attachments"][0]["id"] = identifier
    with pytest.raises(ValueError, match="identity"):
        collect_slim_document_snapshot(connector(monkeypatch, iter([[item]])))


def test_download_failure_does_not_confirm_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    value = connector(monkeypatch, iter([[record()]]))

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("attachment denied")

    monkeypatch.setattr("common.data_source.airtable_connector.requests.get", denied)
    with pytest.raises(PermissionError):
        list(value.load_from_state())


@pytest.mark.parametrize("pages", [[{}], [{"records": None}], [{"records": []}, {}], [{"records": [], "offset": ""}], [{"records": [], "offset": "same"}, {"records": [], "offset": "same"}], []])
def test_raw_incomplete_pages_never_authorize_empty_snapshot(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> None:
    value = connector(monkeypatch, iter(()))
    table = value.airtable_client.table(value.base_id, value.table_name_or_id)
    table.api.iterate_requests = lambda **kwargs: iter(pages)
    with pytest.raises(ValueError):
        collect_slim_document_snapshot(value)


def test_attachment_identity_without_content_metadata_remains_in_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    item = record()
    item["fields"]["attachments"] = [{"id": "att1"}]
    value = connector(monkeypatch, iter([[item]]))
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == ["airtable:rec1:att1"]


def test_real_sdk_pagination_preserves_raw_page_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    value = AirtableConnector("base", "table", batch_size=1)
    value.load_credentials({"airtable_access_token": "scratch-token"})
    calls: list[dict[str, Any]] = []

    def request(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["options"].get("offset") == "next":
            return {"records": []}
        return {"records": [record()], "offset": "next"}

    monkeypatch.setattr(value.airtable_client, "request", request)
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == ["airtable:rec1:att1"]
    assert len(calls) == 2 and calls[1]["options"] == {"offset": "next"}
