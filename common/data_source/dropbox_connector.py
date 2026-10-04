"""Dropbox connector"""

import logging
from datetime import UTC, datetime
from typing import Any

from dropbox import Dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata, FolderMetadata

from common.data_source.config import INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import (
    ConnectorMissingCredentialError,
    ConnectorValidationError,
    InsufficientPermissionsError,
)
from common.data_source.interfaces import LoadConnector, PollConnector, SecondsSinceUnixEpoch, SlimConnectorWithPermSync
from common.data_source.models import Document, GenerateDocumentsOutput, GenerateSlimDocumentOutput, SlimDocument
from common.data_source.utils import get_file_ext

logger = logging.getLogger(__name__)


class DropboxConnector(LoadConnector, PollConnector, SlimConnectorWithPermSync):
    """Dropbox connector for accessing Dropbox files and folders"""

    def __init__(self, batch_size: int = INDEX_BATCH_SIZE) -> None:
        if batch_size < 1:
            raise ValueError("Dropbox batch_size must be positive")
        self.batch_size = batch_size
        self.dropbox_client: Dropbox | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load Dropbox credentials"""
        access_token = credentials.get("dropbox_access_token")
        if not access_token:
            raise ConnectorMissingCredentialError("Dropbox access token is required")

        self.dropbox_client = Dropbox(access_token)
        return None

    def validate_connector_settings(self) -> None:
        """Validate Dropbox connector settings"""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")

        try:
            self.dropbox_client.files_list_folder(path="", limit=1)
        except AuthError as e:
            logger.exception("[Dropbox]: Failed to validate Dropbox credentials")
            raise ConnectorValidationError(f"Dropbox credential is invalid: {e}")
        except ApiError as e:
            if e.error is not None and "insufficient_permissions" in str(e.error).lower():
                raise InsufficientPermissionsError("Your Dropbox token does not have sufficient permissions.")
            raise ConnectorValidationError(f"Unexpected Dropbox error during validation: {e.user_message_text or e}")
        except Exception as e:
            raise ConnectorValidationError(f"Unexpected error during Dropbox settings validation: {e}")

    def _download_file(self, path: str) -> bytes:
        """Download a single file from Dropbox."""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")
        _, resp = self.dropbox_client.files_download(path)
        try:
            return resp.content
        finally:
            resp.close()

    def _get_shared_link(self, path: str) -> str:
        """Create a shared link for a file in Dropbox."""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")

        try:
            shared_links = self.dropbox_client.sharing_list_shared_links(path=path)
            if shared_links.links:
                return shared_links.links[0].url

            link_metadata = self.dropbox_client.sharing_create_shared_link_with_settings(path)
            return link_metadata.url
        except ApiError as err:
            logger.exception(f"[Dropbox]: Failed to create a shared link for {path}: {err}")
            return ""

    def _yield_files_recursive(
        self,
        path: str,
        start: SecondsSinceUnixEpoch | None,
        end: SecondsSinceUnixEpoch | None,
    ) -> GenerateDocumentsOutput:
        """Yield files in batches from a specified Dropbox folder, including subfolders."""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")

        # Collect all files first to count filename occurrences
        all_files: list[FileMetadata] = []
        self._collect_file_entries_recursive(path, start, end, all_files)

        # Count filename occurrences
        filename_counts: dict[str, int] = {}
        for entry in all_files:
            filename_counts[entry.name] = filename_counts.get(entry.name, 0) + 1

        # Process files in batches
        batch: list[Document] = []
        for entry in all_files:
            # Download by stable identity: a path can be replaced after enumeration.
            # Any read failure aborts the run before deletion reconciliation.
            downloaded_file = self._download_file(entry.id)

            batch.append(
                Document(
                    id=f"dropbox:{entry.id}",
                    blob=downloaded_file,
                    source=DocumentSource.DROPBOX,
                    semantic_identifier=self._get_semantic_identifier(entry, filename_counts),
                    extension=get_file_ext(entry.name),
                    doc_updated_at=self._normalize_modified_time(entry.client_modified),
                    size_bytes=entry.size if getattr(entry, "size", None) is not None else len(downloaded_file),
                )
            )

            if len(batch) == self.batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    @staticmethod
    def _normalize_modified_time(modified_time: datetime) -> datetime:
        if modified_time.tzinfo is None:
            return modified_time.replace(tzinfo=UTC)
        return modified_time.astimezone(UTC)

    @staticmethod
    def _get_semantic_identifier(entry: FileMetadata, filename_counts: dict[str, int]) -> str:
        if filename_counts[entry.name] <= 1:
            return entry.name
        relative_path = entry.path_display.lstrip("/")
        return relative_path.replace("/", " / ") if relative_path else entry.name

    def _collect_file_entries_recursive(
        self,
        path: str,
        start: SecondsSinceUnixEpoch | None,
        end: SecondsSinceUnixEpoch | None,
        all_files: list[FileMetadata],
        *,
        visited: set[str] | None = None,
    ) -> None:
        """Enumerate metadata completely; never download a body here."""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")
        visited = set() if visited is None else visited
        if path in visited:
            raise ValueError("Dropbox listing repeated a folder")
        visited.add(path)
        result = self.dropbox_client.files_list_folder(path, recursive=False, include_non_downloadable_files=False, include_deleted=False)
        cursors: set[str] = set()
        while True:
            if not isinstance(result.entries, list) or not isinstance(result.has_more, bool):
                raise ValueError("Incomplete Dropbox listing")
            for entry in result.entries:
                if isinstance(entry, FileMetadata):
                    if not isinstance(entry.id, str) or not entry.id.strip():
                        raise ValueError("Dropbox file has no identity")
                    if start is not None or end is not None:
                        modified = self._normalize_modified_time(entry.client_modified).timestamp()
                        if start is not None and modified <= start:
                            continue
                        if end is not None and modified > end:
                            continue
                    all_files.append(entry)
                elif isinstance(entry, FolderMetadata):
                    if not isinstance(entry.path_lower, str) or not entry.path_lower:
                        raise ValueError("Dropbox folder has no path")
                    self._collect_file_entries_recursive(entry.path_lower, start, end, all_files, visited=visited)
                else:
                    raise ValueError("Unexpected Dropbox metadata entry")
            if not result.has_more:
                break
            cursor = result.cursor
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("Invalid Dropbox pagination cursor")
            cursors.add(cursor)
            result = self.dropbox_client.files_list_folder_continue(cursor)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> GenerateSlimDocumentOutput:
        all_files: list[FileMetadata] = []
        self._collect_file_entries_recursive("", None, None, all_files)
        for offset in range(0, len(all_files), self.batch_size):
            yield [SlimDocument(id=f"dropbox:{entry.id}") for entry in all_files[offset : offset + self.batch_size]]

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> GenerateDocumentsOutput:
        """Poll Dropbox for recent file changes"""
        if self.dropbox_client is None:
            raise ConnectorMissingCredentialError("Dropbox")

        yield from self._yield_files_recursive("", start, end)

    def load_from_state(self) -> GenerateDocumentsOutput:
        """Load files from Dropbox state"""
        return self._yield_files_recursive("", None, None)


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.DEBUG)
    connector = DropboxConnector()
    connector.load_credentials({"dropbox_access_token": os.environ.get("DROPBOX_ACCESS_TOKEN")})
    connector.validate_connector_settings()
    document_batches = connector.load_from_state()
    try:
        first_batch = next(document_batches)
        print(f"Loaded {len(first_batch)} documents in first batch.")
        for doc in first_batch:
            print(f"- {doc.semantic_identifier} ({doc.size_bytes} bytes)")
    except StopIteration:
        print("No documents available in Dropbox.")
