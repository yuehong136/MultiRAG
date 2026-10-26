"""Scoped OneDrive delta enumeration with actual file content and strict cursors.

Only explicitly configured drives are read. The worker uses its durable time
checkpoint; opaque delta links are available to direct connector callers only.
Source ACLs are not local dataset ACLs and deletion synchronization stays off.
"""

import hashlib
import json
import math
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ConfigDict, Field

from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, SecondsSinceUnixEpoch, SlimConnectorWithPermSync
from common.data_source.microsoft_graph import GraphConnection, collection_page, graph_url
from common.data_source.models import ConnectorCheckpoint, ConnectorFailure, Document, SlimDocument

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv"}


def _normalize_folder_path(folder_path: str | None) -> str | None:
    if folder_path is None:
        return None
    if not isinstance(folder_path, str):
        raise ConnectorValidationError("OneDrive folder_path must be a string")
    segments = [part for part in folder_path.strip().split("/") if part]
    if any(part in {".", ".."} or any(ord(char) < 32 for char in part) for part in segments):
        raise ConnectorValidationError("Invalid OneDrive folder_path")
    return "/" + "/".join(segments) if segments else None


class OneDriveCheckpoint(ConnectorCheckpoint):
    model_config = ConfigDict(extra="forbid")

    delta_links: dict[str, str] = Field(default_factory=dict)
    scope: str | None = None


class OneDriveConnector(CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, folder_path: str | None = None, drive_ids: list[str] | None = None, check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ConnectorValidationError("OneDrive batch_size must be a positive integer")
        if not isinstance(drive_ids, list) or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in drive_ids) or len(set(drive_ids)) != len(drive_ids):
            raise ConnectorValidationError("OneDrive drive_ids must be an explicit array of unique nonempty IDs")
        self.batch_size = batch_size
        self.drive_ids = list(drive_ids)
        self.folder_path = _normalize_folder_path(folder_path)
        self._check_active = check_active or (lambda: None)
        self.connection: GraphConnection | None = None
        self._delta_paths: dict[str, str] = {}
        self.scope = hashlib.sha256(json.dumps([self.drive_ids, self.folder_path]).encode()).hexdigest()

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.connection = GraphConnection(credentials, self._check_active)
        return None

    def _connection(self) -> GraphConnection:
        if self.connection is None:
            raise ConnectorMissingCredentialError("OneDrive")
        return self.connection

    def _drive_url(self, drive_id: str) -> str:
        return f"{GRAPH_BASE}/drives/{quote(drive_id, safe='')}"

    def _delta_url(self, drive_id: str) -> str:
        base = self._drive_url(drive_id)
        if drive_id in self._delta_paths:
            return GRAPH_BASE + self._delta_paths[drive_id].removeprefix("/v1.0")
        if not self.folder_path:
            return base + "/root/delta"
        # Resolve the path once and address delta by item identity. Encode each
        # path segment so #/?/% cannot alter the Graph request structure.
        folder = self._connection().json(base + "/root:" + quote(self.folder_path, safe="/"))
        if not isinstance(folder.get("id"), str) or not folder["id"] or not isinstance(folder.get("folder"), dict):
            raise ConnectorValidationError("OneDrive folder_path did not resolve to a folder")
        url = base + "/items/" + quote(folder["id"], safe="") + "/delta"
        self._delta_paths[drive_id] = urlsplit(url).path
        return url

    def validate_connector_settings(self) -> None:
        connection = self._connection()
        for drive_id in self.drive_ids:
            value = connection.json(self._drive_url(drive_id))
            if value.get("id") != drive_id:
                raise ConnectorValidationError("OneDrive drive identity mismatch")
            if self.folder_path:
                self._delta_url(drive_id)
        self._check_active()

    def build_dummy_checkpoint(self) -> OneDriveCheckpoint:
        return OneDriveCheckpoint(has_more=True, scope=self.scope)

    def validate_checkpoint_json(self, checkpoint_json: str) -> OneDriveCheckpoint:
        checkpoint = OneDriveCheckpoint.model_validate_json(checkpoint_json, strict=True)
        self._validate_checkpoint(checkpoint)
        return checkpoint

    def _validate_checkpoint(self, checkpoint: OneDriveCheckpoint) -> None:
        if checkpoint.scope != self.scope or set(checkpoint.delta_links) - set(self.drive_ids):
            raise ConnectorValidationError("OneDrive checkpoint scope mismatch")
        for drive_id, link in checkpoint.delta_links.items():
            self._check_link(drive_id, link)

    def _check_link(self, drive_id: str, link: str) -> None:
        graph_url(link)
        if urlsplit(link).path != urlsplit(self._delta_url(drive_id)).path:
            raise ConnectorValidationError("OneDrive continuation changed drive or folder scope")

    def _inventory(self, drive_id: str, link: str | None = None) -> tuple[list[dict[str, Any]], str]:
        connection = self._connection()
        url: str | None = link or self._delta_url(drive_id)
        visited: set[str] = set()
        items: dict[str, dict[str, Any]] = {}
        delta: str | None = None
        while url:
            self._check_link(drive_id, url)
            if url in visited:
                raise ConnectorValidationError("OneDrive pagination did not advance")
            visited.add(url)
            if len(visited) > 10_000:
                raise ConnectorValidationError("OneDrive page limit exceeded")
            page = connection.json(url)
            for item in collection_page(page):
                # Graph explicitly permits repeated delta IDs: last state wins,
                # including tombstones, before any file is emitted/downloaded.
                items[item["id"]] = item
                if len(items) > 100_000:
                    raise ConnectorValidationError("OneDrive delta inventory exceeds item limit")
            url, delta = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
            if url and delta:
                raise ConnectorValidationError("Ambiguous OneDrive continuation")
            if delta:
                self._check_link(drive_id, delta)
        if not delta:
            raise ConnectorValidationError("Incomplete OneDrive delta enumeration")
        self._check_active()
        return list(items.values()), delta

    @staticmethod
    def _modified(item: dict[str, Any]) -> datetime:
        raw = item.get("lastModifiedDateTime")
        if not isinstance(raw, str):
            raise ConnectorValidationError("Missing OneDrive file modification time")
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorValidationError("Invalid OneDrive file modification time") from exc
        if value.tzinfo is None:
            raise ConnectorValidationError("OneDrive file time has no timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _extension(item: dict[str, Any]) -> str | None:
        if "deleted" in item or "folder" in item:
            return None
        if not isinstance(item.get("file"), dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise ConnectorValidationError("Invalid OneDrive file")
        extension = "." + item["name"].rsplit(".", 1)[-1].lower() if "." in item["name"] else ""
        return extension if extension in SUPPORTED_EXTENSIONS else None

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, OneDriveCheckpoint]:
        if not isinstance(checkpoint, OneDriveCheckpoint):
            raise ConnectorValidationError("OneDrive checkpoint type mismatch")
        self._validate_checkpoint(checkpoint)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ConnectorValidationError("Invalid OneDrive polling window")
        links = dict(checkpoint.delta_links)
        if checkpoint.has_more:
            for drive_id in self.drive_ids:
                items, delta = self._inventory(drive_id, links.get(drive_id))
                for item in items:
                    extension = self._extension(item)
                    if extension is None:
                        continue
                    modified = self._modified(item)
                    if modified.timestamp() > end:
                        raise ConnectorValidationError("OneDrive file time exceeds polling window")
                    if not links.get(drive_id) and modified.timestamp() <= start:
                        continue
                    body = self._connection().read(self._drive_url(drive_id) + "/items/" + quote(item["id"], safe="") + "/content", content=True).content
                    self._check_active()
                    yield Document(
                        id=f"{drive_id}:{item['id']}",
                        source="onedrive",
                        semantic_identifier=item["name"],
                        extension=extension,
                        blob=body,
                        size_bytes=len(body),
                        doc_updated_at=modified,
                        metadata={"drive_id": drive_id, "drive_item_id": item["id"], "web_url": item.get("webUrl", ""), "created_by": item.get("createdBy", {}).get("user", {}).get("displayName", "")},
                    )
                links[drive_id] = delta
        self._check_active()
        # Do not mutate the supplied checkpoint before ingestion succeeds.
        return OneDriveCheckpoint(has_more=False, delta_links=links, scope=self.scope)

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Iterator[list[Document]]:
        batch: list[Document] = []
        for document in self.load_from_checkpoint(start, end, self.build_dummy_checkpoint()):
            if not isinstance(document, Document):
                raise ConnectorValidationError("OneDrive document failure")
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
    ) -> Generator[Document | ConnectorFailure, None, OneDriveCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        # Always a fresh full snapshot, never a delta-only change set.
        batch: list[SlimDocument] = []
        for drive_id in self.drive_ids:
            items, _delta = self._inventory(drive_id)
            for item in items:
                if callback is not None and callback.should_stop():
                    raise RuntimeError("OneDrive enumeration cancelled")
                if self._extension(item) is not None:
                    batch.append(SlimDocument(id=f"{drive_id}:{item['id']}"))
                    if len(batch) >= self.batch_size:
                        self._check_active()
                        yield batch
                        batch = []
        self._check_active()
        if batch:
            yield batch
