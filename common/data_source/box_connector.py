"""Box connector"""

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

from box_sdk_gen import BoxClient

from common.data_source.config import INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import (
    ConnectorMissingCredentialError,
    ConnectorValidationError,
)
from common.data_source.interfaces import LoadConnector, PollConnector, SecondsSinceUnixEpoch
from common.data_source.models import Document, GenerateDocumentsOutput, GenerateSlimDocumentOutput, SlimDocument
from common.data_source.utils import get_file_ext


class BoxConnector(LoadConnector, PollConnector):
    def __init__(self, folder_id: str, batch_size: int = INDEX_BATCH_SIZE, use_marker: bool = True) -> None:
        self.batch_size = batch_size
        self.folder_id = "0" if not folder_id else folder_id
        self.use_marker = use_marker
        self.box_client: BoxClient | None = None

    def load_credentials(self, auth: Any) -> None:
        self.box_client = BoxClient(auth=auth)
        return None

    def validate_connector_settings(self) -> None:
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        try:
            self.box_client.users.get_user_me()
        except Exception as e:
            logging.exception("[Box]: Failed to validate Box credentials")
            raise ConnectorValidationError(f"Unexpected error during Box settings validation: {e}")

    def _iter_files_recursive(
        self,
        folder_id: str,
        relative_folder_path: str = "",
    ) -> Generator[tuple[Any, str], None, None]:
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        result = self.box_client.folders.get_folder_items(folder_id=folder_id, limit=self.batch_size, usemarker=self.use_marker)
        markers: set[str] = set()
        offset = 0

        while True:
            for entry in result.entries:
                if entry.type == "file":
                    file = self.box_client.files.get_file_by_id(entry.id)
                    semantic_identifier = f"{relative_folder_path} / {file.name}" if relative_folder_path else file.name
                    yield file, semantic_identifier
                elif entry.type == "folder":
                    child_relative_path = f"{relative_folder_path} / {entry.name}" if relative_folder_path else entry.name
                    yield from self._iter_files_recursive(
                        folder_id=entry.id,
                        relative_folder_path=child_relative_path,
                    )

            if self.use_marker:
                if not result.next_marker:
                    break
                if result.next_marker in markers:
                    raise ConnectorValidationError("Box listing returned a repeated pagination marker.")
                markers.add(result.next_marker)
                result = self.box_client.folders.get_folder_items(folder_id=folder_id, limit=self.batch_size, marker=result.next_marker, usemarker=True)
            else:
                # Box total_count may include gaps. Advance by the server's
                # page limit rather than the number of returned entries.
                total_count = result.total_count
                page_limit = result.limit
                if not isinstance(total_count, int) or total_count < 0 or not isinstance(page_limit, int) or page_limit <= 0 or result.offset != offset:
                    raise ConnectorValidationError("Box listing returned invalid offset pagination metadata.")
                offset += page_limit
                if offset >= total_count:
                    break
                result = self.box_client.folders.get_folder_items(folder_id=folder_id, limit=self.batch_size, offset=offset, usemarker=False)

    def _yield_files_recursive(
        self,
        folder_id: str,
        start: SecondsSinceUnixEpoch | None,
        end: SecondsSinceUnixEpoch | None,
        relative_folder_path: str = "",
    ) -> GenerateDocumentsOutput:
        batch: list[Document] = []
        for file, semantic_identifier in self._iter_files_recursive(folder_id, relative_folder_path):
            raw_time = getattr(file, "created_at", None) or getattr(file, "content_created_at", None)
            modified_time: SecondsSinceUnixEpoch | None = None
            if raw_time:
                modified_time = self._box_datetime_to_epoch_seconds(raw_time)
                if start is not None and modified_time <= start:
                    continue
                if end is not None and modified_time > end:
                    continue
            content_bytes = self.box_client.downloads.download_file(file.id)
            batch.append(
                Document(
                    id=f"box:{file.id}",
                    blob=content_bytes.read(),
                    source=DocumentSource.BOX,
                    semantic_identifier=semantic_identifier,
                    extension=get_file_ext(file.name),
                    doc_updated_at=modified_time,
                    size_bytes=file.size,
                    metadata=file.metadata,
                )
            )
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> GenerateSlimDocumentOutput:
        """Enumerate the complete folder tree; listing and permission errors propagate."""
        del callback
        batch: list[SlimDocument] = []
        for file, _ in self._iter_files_recursive(folder_id=self.folder_id):
            batch.append(SlimDocument(id=f"box:{file.id}"))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _box_datetime_to_epoch_seconds(self, dt: datetime) -> SecondsSinceUnixEpoch:
        """Convert a Box SDK datetime to Unix epoch seconds (UTC).
        Only supports datetime; any non-datetime should be filtered out by caller.
        """
        if not isinstance(dt, datetime):
            raise TypeError(f"box_datetime_to_epoch_seconds expects datetime, got {type(dt)}")

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)

        return SecondsSinceUnixEpoch(int(dt.timestamp()))

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> GenerateDocumentsOutput:
        return self._yield_files_recursive(folder_id=self.folder_id, start=start, end=end)

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._yield_files_recursive(folder_id=self.folder_id, start=None, end=None)
