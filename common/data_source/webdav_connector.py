"""WebDAV connector"""

import logging
import os
import posixpath
from datetime import UTC, datetime
from typing import Any

from webdav4.client import Client as WebDAVClient
from webdav4.multistatus import MultiStatusResponse

from common.data_source.config import BLOB_STORAGE_SIZE_THRESHOLD, INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError, CredentialExpiredError, InsufficientPermissionsError
from common.data_source.interfaces import LoadConnector, OnyxExtensionType, PollConnector, SlimConnectorWithPermSync
from common.data_source.models import Document, GenerateDocumentsOutput, GenerateSlimDocumentOutput, SecondsSinceUnixEpoch, SlimDocument
from common.data_source.utils import (
    get_file_ext,
    is_accepted_file_ext,
)


class _CompleteWebDAVClient(WebDAVClient):
    """Keep DAV response failures visible before the SDK flattens directory entries."""

    def propfind(self, path: str, **kwargs: Any) -> MultiStatusResponse:
        result = super().propfind(path, **kwargs)
        if result.tree.tag != "{DAV:}multistatus":
            raise ValueError("WebDAV listing is not a DAV multistatus response")
        result.raise_for_status()
        root_path = self.join_url(path).path.rstrip("/") or "/"
        if root_path not in result.responses:
            raise ValueError("WebDAV listing omitted the requested root")
        for response in result.responses.values():
            if response.path_norm != root_path and not response.path_norm.startswith(root_path.rstrip("/") + "/"):
                raise ValueError("WebDAV response is outside the requested directory")
            # webdav4 does not check propstat status before extracting properties.
            for propstat in response.response_xml.findall("{DAV:}propstat"):
                status = (propstat.findtext("{DAV:}status") or "").split()
                if len(status) < 2 or status[1] != "200":
                    raise ValueError("WebDAV listing contains failed or missing property status")
            if response.properties.resource_type not in {"file", "directory"}:
                raise ValueError("WebDAV listing omitted resource type")
        return result


class WebDAVConnector(LoadConnector, PollConnector, SlimConnectorWithPermSync):
    """WebDAV connector for syncing files from WebDAV servers"""

    def __init__(
        self,
        base_url: str,
        remote_path: str = "/",
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        """Initialize WebDAV connector

        Args:
            base_url: Base URL of the WebDAV server (e.g., "https://webdav.example.com")
            remote_path: Remote path to sync from (default: "/")
            batch_size: Number of documents per batch
        """
        self.base_url = base_url.rstrip("/")
        if not remote_path:
            remote_path = "/"
        if not remote_path.startswith("/"):
            remote_path = f"/{remote_path}"
        if remote_path.endswith("/") and remote_path != "/":
            remote_path = remote_path.rstrip("/")
        self.remote_path = remote_path
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.client: WebDAVClient | None = None
        self._allow_images: bool | None = None
        self.size_threshold: int | None = BLOB_STORAGE_SIZE_THRESHOLD

    def _build_extension_type(self) -> OnyxExtensionType:
        extension_type = OnyxExtensionType.Plain | OnyxExtensionType.Document
        if bool(self._allow_images):
            extension_type |= OnyxExtensionType.Multimedia
        return extension_type

    def _is_supported_file(self, file_name: str) -> bool:
        file_ext = get_file_ext(file_name)
        return is_accepted_file_ext(file_ext, self._build_extension_type())

    def set_allow_images(self, allow_images: bool) -> None:
        """Set whether to process images"""
        logging.info(f"Setting allow_images to {allow_images}.")
        self._allow_images = allow_images

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load credentials and initialize WebDAV client

        Args:
            credentials: Dictionary containing 'username' and 'password'

        Returns:
            None

        Raises:
            ConnectorMissingCredentialError: If required credentials are missing
        """
        logging.debug(f"Loading credentials for WebDAV server {self.base_url}")

        username = credentials.get("username")
        password = credentials.get("password")

        if not username or not password:
            raise ConnectorMissingCredentialError("WebDAV requires 'username' and 'password' credentials")

        try:
            # Initialize WebDAV client
            self.client = _CompleteWebDAVClient(base_url=self.base_url, auth=(username, password))
        except Exception as e:
            logging.error(f"Failed to connect to WebDAV server: {e}")
            raise ConnectorMissingCredentialError(f"Failed to authenticate with WebDAV server: {e}")

        return None

    @staticmethod
    def _get_size_bytes(file_info: dict[str, Any]) -> int:
        for key in ("size", "content_length", "getcontentlength"):
            value = file_info.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit() and len(value.strip()) <= 20:
                return int(value.strip())
        # Unknown eligibility must not remove an existing document from the snapshot.
        raise ValueError("WebDAV file has no valid size metadata")

    @staticmethod
    def _modified_at(file_info: dict[str, Any], fallback: datetime) -> datetime:
        value = file_info.get("modified")
        if isinstance(value, datetime):
            modified = value
        elif isinstance(value, str):
            try:
                modified = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                modified = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif value is None:
            modified = fallback
        else:
            raise ValueError("WebDAV file has invalid modified time")
        return modified.replace(tzinfo=UTC) if modified.tzinfo is None else modified.astimezone(UTC)

    def _list_files_recursive(
        self,
        path: str,
        start: datetime,
        end: datetime,
        *,
        filter_by_mtime: bool = True,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Exhaust every directory, propagating errors instead of publishing partial data."""
        if self.client is None:
            raise ConnectorMissingCredentialError("WebDAV client not initialized")
        files: list[tuple[str, dict[str, Any]]] = []
        parent = "/" + path.strip("/")
        items = self.client.ls(path, detail=True)
        if not isinstance(items, list):
            raise ValueError("WebDAV listing is not a list")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("WebDAV listing contains an invalid path")
            item_path = item["name"]
            canonical = "/" + item_path.strip("/")
            if canonical == parent:
                if item.get("type") != "directory":
                    raise ValueError("WebDAV configured path is not a directory")
                continue
            if posixpath.normpath(canonical) != canonical or posixpath.dirname(canonical) != parent or canonical in seen:
                raise ValueError("WebDAV listing contains duplicate or out-of-scope paths")
            seen.add(canonical)
            if item.get("type") == "directory":
                files.extend(self._list_files_recursive(item_path, start, end, filter_by_mtime=filter_by_mtime))
            elif item.get("type") == "file":
                if not self._is_supported_file(os.path.basename(item_path)):
                    continue
                size_bytes = self._get_size_bytes(item)
                if self.size_threshold is not None and size_bytes > self.size_threshold:
                    continue
                if not filter_by_mtime or start < self._modified_at(item, end) <= end:
                    files.append((item_path, item))
            else:
                raise ValueError("WebDAV listing contains unknown resource type")
        return files

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> GenerateSlimDocumentOutput:
        """Enumerate the full configured tree without downloading or filtering by mtime."""
        files = self._list_files_recursive(self.remote_path, datetime(1970, 1, 1, tzinfo=UTC), datetime.now(UTC), filter_by_mtime=False)
        batch: list[SlimDocument] = []
        for file_path, _ in files:
            batch.append(SlimDocument(id=f"webdav:{self.base_url}:{file_path}"))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _yield_webdav_documents(
        self,
        start: datetime,
        end: datetime,
    ) -> GenerateDocumentsOutput:
        """Generate documents from WebDAV server

        Args:
            start: Start datetime for filtering
            end: End datetime for filtering

        Yields:
            Batches of documents
        """
        if self.client is None:
            raise ConnectorMissingCredentialError("WebDAV client not initialized")

        logging.info(f"Searching for files in {self.remote_path} between {start} and {end}")
        files = self._list_files_recursive(self.remote_path, start, end)
        logging.info(f"Found {len(files)} files matching time criteria")

        filename_counts: dict[str, int] = {}
        for file_path, _ in files:
            file_name = os.path.basename(file_path)
            filename_counts[file_name] = filename_counts.get(file_name, 0) + 1

        batch: list[Document] = []
        for file_path, file_info in files:
            file_name = os.path.basename(file_path)

            if not self._is_supported_file(file_name):
                logging.debug(f"Skipping file {file_path} due to unsupported extension.")
                continue

            size_bytes = self._get_size_bytes(file_info)
            if self.size_threshold is not None and size_bytes > self.size_threshold:
                logging.warning(f"{file_name} exceeds size threshold of {self.size_threshold}. Skipping.")
                continue

            try:
                logging.debug(f"Downloading file: {file_path}")
                from io import BytesIO

                buffer = BytesIO()
                self.client.download_fileobj(file_path, buffer)
                blob = buffer.getvalue()

                if blob is None or len(blob) == 0:
                    logging.warning(f"Downloaded content is empty for {file_path}")
                    continue

                modified = self._modified_at(file_info, end)

                if filename_counts.get(file_name, 0) > 1:
                    relative_path = file_path
                    if file_path.startswith(self.remote_path):
                        relative_path = file_path[len(self.remote_path) :]
                    if relative_path.startswith("/"):
                        relative_path = relative_path[1:]
                    semantic_id = relative_path.replace("/", " / ") if relative_path else file_name
                else:
                    semantic_id = file_name

                batch.append(
                    Document(
                        id=f"webdav:{self.base_url}:{file_path}",
                        blob=blob,
                        source=DocumentSource.WEBDAV,
                        semantic_identifier=semantic_id,
                        extension=get_file_ext(file_name),
                        doc_updated_at=modified,
                        size_bytes=size_bytes if size_bytes else 0,
                    )
                )

                if len(batch) == self.batch_size:
                    yield batch
                    batch = []

            except Exception as e:
                logging.exception(f"Error downloading file {file_path}: {e}")
                raise

        if batch:
            yield batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        """Load all documents from WebDAV server

        Yields:
            Batches of documents
        """
        logging.debug(f"Loading documents from WebDAV server {self.base_url}")
        return self._yield_webdav_documents(
            start=datetime(1970, 1, 1, tzinfo=UTC),
            end=datetime.now(UTC),
        )

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> GenerateDocumentsOutput:
        """Poll WebDAV server for updated documents

        Args:
            start: Start timestamp (seconds since Unix epoch)
            end: End timestamp (seconds since Unix epoch)

        Yields:
            Batches of documents
        """
        if self.client is None:
            raise ConnectorMissingCredentialError("WebDAV client not initialized")

        start_datetime = datetime.fromtimestamp(start, tz=UTC)
        end_datetime = datetime.fromtimestamp(end, tz=UTC)

        yield from self._yield_webdav_documents(start_datetime, end_datetime)

    def validate_connector_settings(self) -> None:
        """Validate WebDAV connector settings.

        Validation should exercise the same code-paths used by the connector
        (directory listing / PROPFIND), avoiding exists() which may probe with
        methods that differ across servers.
        """
        if self.client is None:
            raise ConnectorMissingCredentialError("WebDAV credentials not loaded.")

        if not self.base_url:
            raise ConnectorValidationError("No base URL was provided in connector settings.")

        # Normalize directory path: for collections, many servers behave better with trailing '/'
        test_path = self.remote_path or "/"
        if not test_path.startswith("/"):
            test_path = f"/{test_path}"
        if test_path != "/" and not test_path.endswith("/"):
            test_path = f"{test_path}/"

        try:
            # Use the same behavior as real sync: list directory with details (PROPFIND)
            self.client.ls(test_path, detail=True)

        except Exception as e:
            # Prefer structured status codes if present on the exception/response
            status = None
            for attr in ("status_code", "code"):
                v = getattr(e, attr, None)
                if isinstance(v, int):
                    status = v
                    break
            if status is None:
                resp = getattr(e, "response", None)
                v = getattr(resp, "status_code", None)
                if isinstance(v, int):
                    status = v

            # If we can classify by status code, do it
            if status == 401:
                raise CredentialExpiredError("WebDAV credentials appear invalid or expired.")
            if status == 403:
                raise InsufficientPermissionsError(f"Insufficient permissions to access path '{self.remote_path}' on WebDAV server.")
            if status == 404:
                raise ConnectorValidationError(f"Remote path '{self.remote_path}' does not exist on WebDAV server.")

            # Fallback: avoid brittle substring matching that caused false positives.
            # Provide the original exception for diagnosis.
            raise ConnectorValidationError(f"WebDAV validation failed for path '{test_path}': {e!r}")


if __name__ == "__main__":
    credentials_dict = {
        "username": os.environ.get("WEBDAV_USERNAME"),
        "password": os.environ.get("WEBDAV_PASSWORD"),
    }

    credentials_dict = {
        "username": "user",
        "password": "pass",
    }

    connector = WebDAVConnector(
        base_url="http://172.17.0.1:8080/",
        remote_path="/",
    )

    try:
        connector.load_credentials(credentials_dict)
        connector.validate_connector_settings()

        document_batch_generator = connector.load_from_state()
        for document_batch in document_batch_generator:
            print("First batch of documents:")
            for doc in document_batch:
                print(f"Document ID: {doc.id}")
                print(f"Semantic Identifier: {doc.semantic_identifier}")
                print(f"Source: {doc.source}")
                print(f"Updated At: {doc.doc_updated_at}")
                print("---")
            break

    except ConnectorMissingCredentialError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
