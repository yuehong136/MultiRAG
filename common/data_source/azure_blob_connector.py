"""Azure Blob data-source ingestion, separate from application object storage.

Only canonical Azure HTTPS endpoints are supported. Prefixes are literal blob
name prefixes; use a trailing slash when selecting a virtual directory.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import xxhash
from azure.core import MatchConditions
from azure.core.credentials import AzureSasCredential
from azure.core.paging import PageIterator
from azure.storage.blob import ContainerClient

from common.data_source.config import BLOB_STORAGE_SIZE_THRESHOLD, INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, FingerprintConnector, LoadConnector, OnyxExtensionType, PollConnector, SlimConnectorWithPermSync
from common.data_source.models import ConnectorCheckpoint, ConnectorFailure, Document, KeyRecord, SecondsSinceUnixEpoch, SlimDocument
from common.data_source.utils import get_file_ext, is_accepted_file_ext

_SUFFIXES = {"core.windows.net", "core.chinacloudapi.cn", "core.usgovcloudapi.net"}
_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")
_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]")


def _required(values: dict[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorMissingCredentialError(f"Azure Blob ({field} is required)")
    return value


def _container(value: str) -> str:
    if value not in {"$root", "$web"} and (not _CONTAINER.fullmatch(value) or "--" in value):
        raise ConnectorValidationError("Azure Blob container_name is invalid")
    return value


def _account_url(account: str, suffix: str = "core.windows.net") -> str:
    if not _ACCOUNT.fullmatch(account) or suffix not in _SUFFIXES:
        raise ConnectorValidationError("Azure Blob account or endpoint suffix is invalid")
    return f"https://{account}.blob.{suffix}"


def _split_url(value: str) -> tuple[str, str]:
    try:
        url = urlsplit(value)
        account, marker, suffix = (url.hostname or "").partition(".blob.")
        account_url = _account_url(account, suffix)
        if url.scheme != "https" or url.netloc != url.hostname or not marker or url.query or url.fragment:
            raise ValueError
        name = _container(url.path.removeprefix("/"))
        if value != account_url + "/" + name:
            raise ValueError
        return account_url, name
    except (ValueError, ConnectorValidationError):
        raise ConnectorValidationError("Azure Blob container_url must be a canonical Azure HTTPS container URL without a SAS query") from None


def _sas(value: str) -> AzureSasCredential:
    token = value.removeprefix("?")
    if not parse_qs(token).get("sig") or any(char.isspace() for char in token) or "://" in token or "#" in token:
        raise ConnectorValidationError("Azure Blob SAS token is invalid")
    return AzureSasCredential(token)


def _connection(values: dict[str, Any], mode: str) -> tuple[str, str, str | AzureSasCredential]:
    if mode == "sas_token":
        account_url, container = _split_url(_required(values, "container_url"))
        return account_url, container, _sas(_required(values, "sas_token"))
    container = _container(_required(values, "container_name"))
    if mode == "account_key":
        return _account_url(_required(values, "account_name")), container, _required(values, "account_key")
    if mode != "connection_string":
        raise ConnectorValidationError("Azure Blob auth_mode must be account_key, connection_string or sas_token")
    fields: dict[str, str] = {}
    for pair in _required(values, "connection_string").split(";"):
        if not pair:
            continue
        key, separator, value = pair.partition("=")
        key = key.lower()
        if not separator or key in fields or key not in {"accountname", "accountkey", "sharedaccesssignature", "defaultendpointsprotocol", "endpointsuffix", "blobendpoint"}:
            raise ConnectorValidationError("Azure Blob connection string has invalid or unsupported fields")
        fields[key] = value
    if fields.get("defaultendpointsprotocol", "https") != "https":
        raise ConnectorValidationError("Azure Blob requires HTTPS")
    account_url = _account_url(_required(fields, "accountname"), fields.get("endpointsuffix", "core.windows.net"))
    if "blobendpoint" in fields and fields["blobendpoint"].rstrip("/") != account_url:
        raise ConnectorValidationError("Azure Blob connection string endpoint must match its Azure account")
    if bool(fields.get("accountkey")) == bool(fields.get("sharedaccesssignature")):
        raise ConnectorMissingCredentialError("Azure Blob (exactly one AccountKey or SharedAccessSignature is required)")
    credential = fields["accountkey"] if fields.get("accountkey") else _sas(fields["sharedaccesssignature"])
    return account_url, container, credential


def _version(properties: Any) -> tuple[str, datetime, int]:
    etag, modified, size = properties.etag, properties.last_modified, properties.size
    if not isinstance(etag, str) or not etag.strip().strip('"') or not isinstance(modified, datetime) or modified.tzinfo is None or type(size) is not int or size < 0:
        raise ConnectorValidationError("Azure Blob returned incomplete version metadata")
    return etag, modified.astimezone(UTC), size


class AzureBlobCheckpoint(ConnectorCheckpoint):
    """One complete paginated inventory; never publish a partial checkpoint."""


class AzureBlobConnector(LoadConnector, PollConnector, FingerprintConnector, CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, prefix: str = "", allow_images: bool = False, auth_mode: str | None = None, check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ConnectorValidationError("Azure Blob batch_size must be an integer between 1 and 1000")
        if not isinstance(prefix, str) or type(allow_images) is not bool:
            raise ConnectorValidationError("Azure Blob prefix must be a string and allow_images must be a boolean")
        if not isinstance(auth_mode, str) or auth_mode not in {"account_key", "connection_string", "sas_token"}:
            raise ConnectorValidationError("Azure Blob auth_mode must be account_key, connection_string or sas_token")
        self.batch_size, self.prefix, self.allow_images, self.auth_mode = batch_size, prefix, allow_images, auth_mode
        self.check_active = check_active or (lambda: None)
        self._container_client: ContainerClient | None = None
        self._listing: dict[str, Any] = {}
        self._connection_args: tuple[str, str, str | AzureSasCredential] | None = None

    @classmethod
    def build_connector(cls, config: dict[str, Any], **kwargs: Any) -> AzureBlobConnector:
        """One static config contract for saves, workers and direct callers."""
        if config.get("sync_deleted_files", False) is not False:
            raise ConnectorValidationError("Azure Blob deletion synchronization is not supported")
        connector = cls(
            batch_size=config.get("batch_size", INDEX_BATCH_SIZE), prefix=config.get("prefix", ""), allow_images=config.get("allow_images", False), auth_mode=config.get("auth_mode"), **kwargs
        )
        credentials = config.get("credentials")
        if not isinstance(credentials, dict):
            raise ConnectorMissingCredentialError("Azure Blob")
        connector.load_credentials(credentials)
        return connector

    def load_credentials(self, credentials: dict[str, Any]) -> None:
        """Validate credentials without constructing a transport or issuing IO."""
        self._connection_args = None
        self.close()
        self._connection_args = _connection(credentials, self.auth_mode)

    def _client(self) -> ContainerClient:
        self.check_active()
        if self._connection_args is None:
            raise ConnectorMissingCredentialError("Azure Blob")
        if self._container_client is None:
            try:
                account_url, container, credential = self._connection_args
                self._container_client = ContainerClient(
                    account_url, container, credential=credential, permit_redirects=False, retry_total=2, retry_connect=2, retry_read=2, retry_status=2, connection_timeout=5, read_timeout=15
                )
            except Exception:
                raise ConnectorValidationError("Azure Blob client initialization failed") from None
        return self._container_client

    def close(self) -> None:
        active_exception = sys.exc_info()[0] is not None
        client, self._container_client = self._container_client, None
        self._listing = {}
        if client is not None:
            try:
                client.close()
            except Exception:
                if active_exception:
                    logging.warning("Azure Blob client close failed")
                else:
                    raise ConnectorValidationError("Azure Blob client close failed") from None

    @contextmanager
    def session(self) -> Iterator[None]:
        try:
            yield
        finally:
            self.close()

    def validate_connector_settings(self) -> None:
        with self.session():
            try:
                self._client().get_container_properties(timeout=15)
                # Container properties alone do not prove permission to list.
                next(iter(self._client().list_blobs(name_starts_with=self.prefix, results_per_page=1, timeout=15)), None)
            except Exception:
                raise ConnectorValidationError("Azure Blob container validation failed; check credentials, container and list permissions") from None

    def _supported(self, name: str) -> bool:
        types = OnyxExtensionType.Plain | OnyxExtensionType.Document
        if self.allow_images:
            types |= OnyxExtensionType.Multimedia
        extension = get_file_ext(name)
        return not name.endswith("/") and (is_accepted_file_ext(extension, types) or extension in {".doc", ".xls", ".ppt", ".htm"})

    def _fingerprint(self, etag: str) -> str:
        if self._connection_args is None:
            raise ConnectorMissingCredentialError("Azure Blob")
        account_url, container, _ = self._connection_args
        # An ETag is a version of one resource, not a globally unique token.
        # Editing the source account/container must invalidate prior versions.
        version = "\0".join((account_url, container, etag.strip().strip('"')))
        return xxhash.xxh128(version.encode()).hexdigest()

    def list_keys(self) -> Iterator[KeyRecord]:
        inventory: dict[str, Any] = {}
        try:
            # ItemPaged annotates by_page() as Iterator, but Azure's concrete
            # PageIterator exposes the continuation marker used for resumption.
            pages = cast(PageIterator, self._client().list_blobs(name_starts_with=self.prefix, timeout=15).by_page())
            seen_tokens: set[str] = set()
            for page in pages:
                self.check_active()
                token = pages.continuation_token
                if token:
                    if token in seen_tokens:
                        raise ConnectorValidationError("Azure Blob returned a repeated continuation marker")
                    seen_tokens.add(token)
                for props in page:
                    name = props.name
                    if not isinstance(name, str) or not name.startswith(self.prefix):
                        raise ConnectorValidationError("Azure Blob returned an out-of-scope name")
                    # Azure's SDK preserves slashes in blob URLs, while its
                    # HTTP transport normalizes dot segments. Refuse names
                    # that could address another container or storage prefix.
                    if any(part in {".", ".."} for part in name.split("/")):
                        raise ConnectorValidationError("Azure Blob returned an unsafe path segment")
                    if not self._supported(name):
                        continue
                    _version(props)
                    if name in inventory:
                        raise ConnectorValidationError("Azure Blob returned duplicate names")
                    inventory[name] = props
        except Exception:
            self._listing = {}
            raise ConnectorValidationError("Azure Blob inventory failed; retry the complete synchronization") from None
        self._listing = inventory
        for name, props in inventory.items():
            yield KeyRecord(key=name, fingerprint=self._fingerprint(props.etag))

    def get_value(self, key: str) -> Document | None:
        self.check_active()
        if key not in self._listing:
            raise ConnectorValidationError("Azure Blob key was not in the complete inventory")
        props = self._listing[key]
        etag, modified, size = _version(props)
        if size > BLOB_STORAGE_SIZE_THRESHOLD:
            return None
        client = self._client()
        try:
            blob = client.get_blob_client(key)
            download = blob.download_blob(etag=etag, match_condition=MatchConditions.IfNotModified, max_concurrency=1, timeout=15)
            fetched_etag, fetched_modified, fetched_size = _version(download.properties)
            if (fetched_etag, fetched_modified, fetched_size) != (etag, modified, size):
                raise ConnectorValidationError("Azure Blob version changed during synchronization")
            data = download.readall()
            self.check_active()
            if len(data) != size:
                raise ConnectorValidationError("Azure Blob download size differs from its version metadata")
        except Exception:
            # Includes 404/412: a vanished or changed blob must not advance the
            # watermark, and raw SDK errors may contain credential-bearing URLs.
            raise ConnectorValidationError("Azure Blob download failed or changed version; retry synchronization") from None
        return Document(
            id=key,
            source=DocumentSource.AZURE_BLOB,
            semantic_identifier=key,
            extension=get_file_ext(key),
            blob=data,
            doc_updated_at=modified,
            size_bytes=len(data),
            fingerprint=self._fingerprint(etag),
            metadata={"container": client.container_name, "prefix": self.prefix, "etag": etag},
        )

    def _iter_documents(self, start: float | None = None, end: float | None = None, checkpoint: ConnectorCheckpoint | None = None) -> Generator[list[Document], None, None]:
        with self.session():
            batch: list[Document] = []
            for record in self.list_keys():
                modified = self._listing[record.key].last_modified.timestamp()
                if (start is not None and modified <= start) or (end is not None and modified > end):
                    continue
                if (doc := self.get_value(record.key)) is not None:
                    batch.append(doc)
                if len(batch) >= self.batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch
            if checkpoint is not None:
                checkpoint.has_more = False

    def load_from_state(self) -> Generator[list[Document], None, None]:
        return self._iter_documents()

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        return self._iter_documents(start, end)

    def build_dummy_checkpoint(self) -> AzureBlobCheckpoint:
        return AzureBlobCheckpoint()

    def validate_checkpoint_json(self, checkpoint_json: str) -> AzureBlobCheckpoint:
        return AzureBlobCheckpoint.model_validate_json(checkpoint_json)

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        for batch in self._iter_documents(start, end, checkpoint):
            yield from batch
        return checkpoint

    def load_from_checkpoint_with_perm_sync(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint
    ) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        with self.session():
            batch: list[SlimDocument] = []
            for record in self.list_keys():
                if callback:
                    callback(record.key, record.key)
                batch.append(SlimDocument(id=record.key))
                if len(batch) >= self.batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch
