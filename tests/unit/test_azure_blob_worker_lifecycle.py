"""The Azure worker drains synchronous SDK requests before returning cancellation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import pytest
from azure.core.pipeline.transport import HttpRequest, HttpResponse
from azure.storage.blob import ContainerClient
from sqlalchemy.orm import Session

from common.data_source import azure_blob_connector as azure
from common.data_source.exceptions import ConnectorValidationError
from core.svr import sync_data_source as sync
from tests.support.azure_blob_source_transport import BlobSourceTransport


async def test_repeated_cancel_drains_get_and_closes_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockingTransport(BlobSourceTransport):
        def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
            if "restype" not in urlsplit(request.url).query:
                entered.set()
                assert release.wait(timeout=5), "test did not release the controlled GET"
            return super().send(request, **kwargs)

    transport = BlockingTransport()

    @contextmanager
    def database() -> Iterator[Session]:
        with Session() as session:
            yield session

    def client(*args: Any, **kwargs: Any) -> ContainerClient:
        return ContainerClient(*args, **{**kwargs, "transport": transport, "retry_total": 0})

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("cancelled GET must not persist any document")

    monkeypatch.setattr(azure, "ContainerClient", client)
    monkeypatch.setattr(sync, "db_connection", database)
    monkeypatch.setattr(sync.SyncLogsService, "raise_if_cancelled", lambda *args: None)
    monkeypatch.setattr(sync.DocumentService, "list_doc_headers_by_kb_and_source_type", lambda *args: [])
    monkeypatch.setattr(sync.DocumentService, "list_id_content_hash_map_by_kb_and_source_type", lambda *args: {})
    monkeypatch.setattr(sync.SyncLogsService, "duplicate_and_parse", forbidden)
    conf = {"auth_mode": "account_key", "prefix": "docs/", "credentials": {"account_name": "ownedaccount", "account_key": "Y29udHJvbGxlZC1rZXk=", "container_name": "container"}}
    task = asyncio.create_task(sync.AzureBlob(conf)._run_sync_task_logic({"id": "task", "kb_id": "kb", "connector_id": "connector", "poll_range_start": None, "reindex": "0"}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not transport.closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert transport.closed and transport.close_count == 1


def test_close_failure_keeps_original_business_error_and_clears_identity(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class FailingClose(BlobSourceTransport):
        def close(self) -> None:
            super().close()
            raise RuntimeError("synthetic-close-secret")

    transport = FailingClose()

    def client(*args: Any, **kwargs: Any) -> ContainerClient:
        return ContainerClient(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(azure, "ContainerClient", client)
    connector = azure.AzureBlobConnector(auth_mode="account_key")
    credentials = {"account_name": "ownedaccount", "account_key": "Y29udHJvbGxlZC1rZXk=", "container_name": "container"}
    connector.load_credentials(credentials)
    with pytest.raises(ValueError, match="business error"), connector.session():
        connector._client()
        raise ValueError("business error")
    assert "synthetic-close-secret" not in caplog.text
    connector.load_credentials(credentials)
    connector._client()
    with pytest.raises(ConnectorValidationError, match="close failed"):
        connector.load_credentials({})
    assert connector._connection_args is None
