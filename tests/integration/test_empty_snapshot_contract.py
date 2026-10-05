"""Complete/failed snapshots and a source object arriving between list and ingest."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from common.constants import FileSource
from common.data_source.models import Document, SlimDocument
from core.svr.sync_data_source import SyncBase
from tests.support.connector_deleted_sync import assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("mode", ["listing-error", "partial", "empty", "concurrent", "disabled"])
async def test_snapshot_outcome_readback(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    class Source:
        listed = False

        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            if mode == "partial":
                yield [SlimDocument(id="retained")]
            if mode in {"listing-error", "partial"}:
                raise PermissionError("controlled enumeration failure")
            self.listed = True

        def documents(self) -> Iterator[list[Document]]:
            if mode == "concurrent":
                assert self.listed
                yield [Document(id="new", source=FileSource.S3, semantic_identifier="new.txt", extension="txt", blob=b"new body", size_bytes=8, doc_updated_at=datetime(2026, 1, 1, tzinfo=UTC))]

    class Driver(SyncBase):
        SOURCE_NAME = FileSource.S3

        async def _generate(self, task: dict[str, Any]) -> Iterator[list[Document]]:
            self.connector = Source()
            return self.connector.documents()

    await assert_deleted_sync(connector_sync_api, monkeypatch, Driver, FileSource.S3, {name: name for name in ["retained", "stale", "new"]}, {"sync_deleted_files": mode != "disabled"}, mode)
