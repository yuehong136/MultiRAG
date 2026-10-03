import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import requests
from pyairtable import Api as AirtableApi

from common.data_source.config import AIRTABLE_CONNECTOR_SIZE_THRESHOLD, INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError
from common.data_source.interfaces import IndexingHeartbeatInterface, LoadConnector, PollConnector, SlimConnectorWithPermSync
from common.data_source.models import Document, GenerateDocumentsOutput, GenerateSlimDocumentOutput, SecondsSinceUnixEpoch, SlimDocument
from common.data_source.utils import extract_size_bytes, get_file_ext


class AirtableClientNotSetUpError(PermissionError):
    def __init__(self) -> None:
        super().__init__("Airtable client is not set up. Did you forget to call load_credentials()?")


class AirtableConnector(LoadConnector, PollConnector, SlimConnectorWithPermSync):
    """
    Lightweight Airtable connector.

    This connector ingests Airtable attachments as raw blobs without
    parsing file content or generating text/image sections.
    """

    def __init__(
        self,
        base_id: str,
        table_name_or_id: str,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.base_id = base_id
        self.table_name_or_id = table_name_or_id
        self.batch_size = batch_size
        self._airtable_client: AirtableApi | None = None
        self.size_threshold = AIRTABLE_CONNECTOR_SIZE_THRESHOLD

    def _iter_attachment_entries(self) -> Generator[tuple[str, dict[str, Any], str | None], None, None]:
        """Share source identities between ingestion and complete, paginated listing."""
        if not self._airtable_client:
            raise ConnectorMissingCredentialError("Airtable credentials not loaded")
        table = self.airtable_client.table(self.base_id, self.table_name_or_id)
        seen_offsets: set[str] = set()
        received_page = False
        # Table.iterate() defaults missing records to []; retain the raw envelope
        # so an incomplete response cannot authorize source deletions.
        for page in table.api.iterate_requests(method="get", url=table.urls.records, fallback=("post", table.urls.records_post)):
            received_page = True
            if not isinstance(page, dict) or not isinstance(page.get("records"), list):
                raise ValueError("Incomplete Airtable records page")
            offset = page.get("offset")
            if offset is not None:
                if not isinstance(offset, str) or not offset.strip() or offset in seen_offsets:
                    raise ValueError("Invalid Airtable pagination progress")
                seen_offsets.add(offset)
            for record in page["records"]:
                if not isinstance(record, dict):
                    raise ValueError("Incomplete Airtable record during attachment enumeration")
                record_id, fields = record.get("id"), record.get("fields")
                if not isinstance(record_id, str) or not record_id.strip() or not isinstance(fields, dict):
                    raise ValueError("Incomplete Airtable record during attachment enumeration")
                for value in fields.values():
                    if not isinstance(value, list):
                        continue
                    for attachment in value:
                        # Multi-select/link fields contain strings; collaborator fields
                        # contain dictionaries, but have no attachment metadata.
                        if not isinstance(attachment, dict) or not (any(key in attachment for key in ("filename", "url")) or str(attachment.get("id", "")).startswith("att")):
                            continue
                        attachment_id = attachment.get("id")
                        if not isinstance(attachment_id, str) or not attachment_id.strip():
                            raise ValueError("Incomplete Airtable attachment identity")
                        yield f"airtable:{record_id}:{attachment_id}", attachment, record.get("createdTime")
        if not received_page:
            raise ValueError("Airtable inventory did not receive a records page")

    def retrieve_all_slim_docs_perm_sync(self, callback: IndexingHeartbeatInterface | None = None) -> GenerateSlimDocumentOutput:
        batch: list[SlimDocument] = []
        for doc_id, _, _ in self._iter_attachment_entries():
            if callback and callback.should_stop():
                raise RuntimeError("Airtable attachment enumeration cancelled")
            batch.append(SlimDocument(id=doc_id))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    # -------------------------
    # Credentials
    # -------------------------
    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self._airtable_client = AirtableApi(credentials["airtable_access_token"])
        return None

    @property
    def airtable_client(self) -> AirtableApi:
        if not self._airtable_client:
            raise AirtableClientNotSetUpError()
        return self._airtable_client

    # -------------------------
    # Core logic
    # -------------------------
    def load_from_state(self) -> GenerateDocumentsOutput:
        """
        Fetch all Airtable records and ingest attachments as raw blobs.

        Each attachment is converted into a single Document(blob=...).
        """
        if not self._airtable_client:
            raise ConnectorMissingCredentialError("Airtable credentials not loaded")

        batch: list[Document] = []
        for doc_id, attachment, created_time in self._iter_attachment_entries():
            url = attachment.get("url")
            filename = attachment.get("filename")
            attachment_id = attachment.get("id")

            if not url or not filename or not attachment_id or not created_time:
                raise ValueError("Incomplete Airtable attachment content metadata")

            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                content = resp.content
            except Exception:
                logging.exception("Failed to download Airtable attachment %s", doc_id)
                raise
            size_bytes = extract_size_bytes(attachment)
            if self.size_threshold is not None and isinstance(size_bytes, int) and size_bytes > self.size_threshold:
                logging.warning(f"{filename} exceeds size threshold of {self.size_threshold}. Skipping.")
                continue
            batch.append(
                Document(
                    id=doc_id,
                    blob=content,
                    source=DocumentSource.AIRTABLE,
                    semantic_identifier=filename,
                    extension=get_file_ext(filename),
                    size_bytes=size_bytes if size_bytes else 0,
                    doc_updated_at=datetime.strptime(created_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
                )
            )

            if len(batch) >= self.batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        """Poll source to get documents"""
        start_dt = datetime.fromtimestamp(start, tz=UTC)
        end_dt = datetime.fromtimestamp(end, tz=UTC)

        for batch in self.load_from_state():
            filtered: list[Document] = []

            for doc in batch:
                if not doc.doc_updated_at:
                    continue

                doc_dt = doc.doc_updated_at.astimezone(UTC)

                if start_dt <= doc_dt < end_dt:
                    filtered.append(doc)

            if filtered:
                yield filtered


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.DEBUG)
    connector = AirtableConnector("xxx", "xxx")
    connector.load_credentials({"airtable_access_token": os.environ.get("AIRTABLE_ACCESS_TOKEN")})
    connector.validate_connector_settings()
    document_batches = connector.load_from_state()
    try:
        first_batch = next(document_batches)
        print(f"Loaded {len(first_batch)} documents in first batch.")
        for doc in first_batch:
            print(f"- {doc.semantic_identifier} ({doc.size_bytes} bytes)")
    except StopIteration:
        print("No documents available in Dropbox.")
