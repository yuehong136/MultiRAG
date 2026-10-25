"""Read SharePoint document libraries through the Microsoft Graph SDK.

App-only credentials need admin-consented Sites.Read.All and Files.Read.All.
Permission visibility is not an authoritative deletion inventory; the worker
keeps this source outside the deleted-file synchronization registry.
"""

import math
import re
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import msal
import requests
from office365.graph_client import GraphClient
from office365.graph_request import GraphRequest
from office365.runtime.client_object_collection import ClientObjectCollection
from office365.runtime.http.request_options import RequestOptions
from office365.runtime.queries.client_query import ClientQuery

from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, SecondsSinceUnixEpoch, SlimConnectorWithPermSync
from common.data_source.models import ConnectorCheckpoint, ConnectorFailure, Document, SlimDocument

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


class _ReadOnlyGraphRequest(GraphRequest):
    """Keep SDK mapping/authentication, with bounded reads and strict pages."""

    def __init__(self, check_active: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._check_active = check_active

    def execute_request_direct(self, request: RequestOptions) -> requests.Response:
        target = urlsplit(request.url)
        if request.method != "GET" or target.scheme != "https" or target.netloc != "graph.microsoft.com":
            raise ConnectorValidationError("Unexpected SharePoint Graph request")
        self.beforeExecute.notify(request)
        self._check_active()
        remaining = getattr(self._check_active, "remaining_seconds", lambda: 60.0)()
        response = requests.get(request.url, headers=request.headers, auth=request.auth, verify=request.verify, proxies=request.proxies, timeout=(min(10.0, remaining), min(60.0, remaining)))
        self._check_active()
        response.raise_for_status()
        return response

    def process_response(self, response: requests.Response, query: ClientQuery) -> None:
        if isinstance(query.return_type, ClientObjectCollection):
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
                raise ConnectorValidationError("Invalid SharePoint collection response")
            if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in payload["value"]):
                raise ConnectorValidationError("Invalid SharePoint collection item")
            for item in payload["value"]:
                if "file" in item:
                    modified = item.get("lastModifiedDateTime")
                    if not isinstance(modified, str):
                        raise ConnectorValidationError("Missing SharePoint file modification time")
                    try:
                        timestamp = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ConnectorValidationError("Invalid SharePoint file modification time") from exc
                    if timestamp.tzinfo is None:
                        raise ConnectorValidationError("SharePoint file modification time has no timezone")
            link = payload.get("@odata.nextLink")
            if link is not None and (not isinstance(link, str) or not link):
                raise ConnectorValidationError("Invalid SharePoint next page")
        super().process_response(response, query)


class _ReadOnlyGraphClient(GraphClient):
    def __init__(self, token_callback: Callable[[], dict[str, Any]], check_active: Callable[[], None]) -> None:
        super().__init__(token_callback)
        self._check_active = check_active

    def pending_request(self) -> GraphRequest:
        if self._pending_request is None:
            self._pending_request = _ReadOnlyGraphRequest(check_active=self._check_active, tenant=self._tenant, environment=self._environment)
            self._pending_request.with_access_token(self._token_callback)
            self._pending_request.beforeExecute += self._build_specific_query
        return self._pending_request


class SharePointConnector(CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ConnectorValidationError("SharePoint batch_size must be a positive integer")
        self.batch_size = batch_size
        self.graph_client: GraphClient | None = None
        self._site_url: str | None = None
        self._check_active = check_active or (lambda: None)

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Configure lazy MSAL auth; constructing the connector does no IO."""
        required = ("tenant_id", "client_id", "client_secret", "site_url")
        if any(not isinstance(credentials.get(key), str) or not credentials[key].strip() for key in required):
            raise ConnectorMissingCredentialError("SharePoint credentials are incomplete")
        tenant_id, client_id, client_secret, site_url = (credentials[key] for key in required)
        site = urlsplit(site_url)
        if site.scheme != "https" or not site.hostname or site.username or site.password or site.query or site.fragment:
            raise ConnectorValidationError("SharePoint site_url must be an HTTPS site URL")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", tenant_id):
            raise ConnectorValidationError("Invalid SharePoint tenant_id")
        self._site_url = site_url
        app: Any = None

        def acquire_token() -> dict[str, Any]:
            nonlocal app
            self._check_active()
            if app is None:
                app = msal.ConfidentialClientApplication(
                    client_id=client_id,
                    client_credential=client_secret,
                    authority=f"https://login.microsoftonline.com/{tenant_id}",
                    timeout=min(30.0, getattr(self._check_active, "remaining_seconds", lambda: 30.0)()),
                )
            token = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
            self._check_active()
            if not isinstance(token, dict) or not token.get("access_token"):
                raise ConnectorMissingCredentialError("Failed to acquire SharePoint access token")
            return token

        self.graph_client = _ReadOnlyGraphClient(acquire_token, self._check_active)
        self.graph_client.pending_request().beforeExecute += lambda _request: self._check_active()
        return None

    def _site(self) -> Any:
        if self.graph_client is None or self._site_url is None:
            raise ConnectorMissingCredentialError("SharePoint")
        self._check_active()
        site = self.graph_client.sites.get_by_url(self._site_url).execute_query()
        if not isinstance(site.id, str) or not site.id:
            raise ConnectorValidationError("Failed to resolve SharePoint site")
        return site

    def validate_connector_settings(self) -> None:
        self._site()

    def _iter_collection(self, collection: Any) -> Iterator[Any]:
        """SDK iteration follows nextLink only with paged mode enabled."""
        next_links: set[str] = set()
        identifiers: set[str] = set()

        def page_loaded(page: Any) -> None:
            self._check_active()
            link = page._next_request_url
            if link is not None:
                if link in next_links:
                    raise ConnectorValidationError("SharePoint pagination did not advance")
                next_links.add(link)

        loaded = collection.paged(self.batch_size, page_loaded).get().execute_query()
        for item in loaded:
            self._check_active()
            if not isinstance(item.id, str) or not item.id or item.id in identifiers:
                raise ConnectorValidationError("Duplicate or missing SharePoint item identity")
            identifiers.add(item.id)
            yield item

    def _iter_drives(self) -> Iterator[Any]:
        yield from self._iter_collection(self._site().drives)

    def _walk_files(self, root_item: Any) -> Iterator[Any]:
        stack = [root_item]
        seen: set[str] = set()
        while stack:
            for child in self._iter_collection(stack.pop().children):
                if child.id in seen:
                    raise ConnectorValidationError("SharePoint traversal repeated an item")
                seen.add(child.id)
                properties = child.properties
                if "folder" in properties:
                    stack.append(child)
                elif "file" in properties:
                    yield child
                else:
                    raise ConnectorValidationError("Unsupported SharePoint drive item")

    @staticmethod
    def _modified_dt(drive_item: Any) -> datetime:
        value = drive_item.properties.get("lastModifiedDateTime")
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ConnectorValidationError("Invalid SharePoint file modification time") from exc
        if not isinstance(value, datetime):
            raise ConnectorValidationError("Missing or invalid SharePoint file modification time")
        # The pinned SDK parses Graph's UTC Z timestamps into naive datetime.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _composite_doc_id(drive_id: str, drive_item: Any) -> str:
        if not drive_id or not isinstance(drive_item.id, str) or not drive_item.id:
            raise ConnectorValidationError("Missing SharePoint file identity")
        return f"{drive_id}:{drive_item.id}"

    def _drive_item_to_document(self, drive_item: Any, drive_id: str, drive_name: str) -> Document:
        self._check_active()
        name = drive_item.name
        if not isinstance(name, str) or not name:
            raise ConnectorValidationError("Missing SharePoint file name")
        modified = self._modified_dt(drive_item)
        blob = drive_item.get_content().execute_query().value
        self._check_active()
        if not isinstance(blob, bytes):
            raise ConnectorValidationError("Invalid SharePoint file content")
        metadata = {"drive": drive_name, "drive_id": drive_id, "drive_item_id": drive_item.id}
        web_url = drive_item.properties.get("webUrl")
        if web_url:
            metadata["web_url"] = web_url
        return Document(
            id=self._composite_doc_id(drive_id, drive_item),
            source="sharepoint",
            semantic_identifier=name,
            extension="." + name.rsplit(".", 1)[1] if "." in name else "",
            blob=blob,
            size_bytes=len(blob),
            doc_updated_at=modified,
            metadata=metadata,
        )

    def _generate_documents(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Iterator[Document]:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ConnectorValidationError("Invalid SharePoint polling window")
        for drive in self._iter_drives():
            for drive_item in self._walk_files(drive.root):
                if start < self._modified_dt(drive_item).timestamp() <= end:
                    document = self._drive_item_to_document(drive_item, drive.id, drive.name or "")
                    self._check_active()
                    yield document
        self._check_active()

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        if checkpoint.has_more:
            yield from self._generate_documents(start, end)
        return ConnectorCheckpoint(has_more=False)

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Iterator[list[Document]]:
        batch: list[Document] = []
        for document in self._generate_documents(start, end):
            batch.append(document)
            if len(batch) >= self.batch_size:
                self._check_active()
                yield batch
                batch = []
        self._check_active()
        if batch:
            yield batch

    def load_from_checkpoint_with_perm_sync(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint
    ) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        # Source ACLs are not mapped into local dataset authorization.
        return self.load_from_checkpoint(start, end, checkpoint)

    def build_dummy_checkpoint(self) -> ConnectorCheckpoint:
        return ConnectorCheckpoint(has_more=True)

    def validate_checkpoint_json(self, checkpoint_json: str) -> ConnectorCheckpoint:
        return ConnectorCheckpoint.model_validate_json(checkpoint_json, strict=True)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        batch: list[SlimDocument] = []
        for drive in self._iter_drives():
            for drive_item in self._walk_files(drive.root):
                if callback is not None and callback.should_stop():
                    raise RuntimeError("SharePoint enumeration cancelled")
                batch.append(SlimDocument(id=self._composite_doc_id(drive.id, drive_item)))
                if len(batch) >= self.batch_size:
                    yield batch
                    batch = []
        if batch:
            yield batch
