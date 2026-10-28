"""Explicit mailbox/folder Outlook reads using the bounded Graph transport.

Workers use their existing modification-time watermark. Delta checkpoints are
available to direct callers; they are never advertised as durable worker state.
Attachments and source ACLs are metadata only; deletion synchronization is off.
"""

import hashlib
import html
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


def _identity(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip() and value not in {".", ".."} and len(value) <= 1024 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


class OutlookCheckpoint(ConnectorCheckpoint):
    model_config = ConfigDict(extra="forbid")

    delta_links: dict[str, str] = Field(default_factory=dict)
    scope: str | None = None


class OutlookConnector(CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, folder: str = "inbox", user_ids: list[str] | None = None, check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ConnectorValidationError("Outlook batch_size must be a positive integer")
        if not isinstance(user_ids, list) or any(not _identity(item) for item in user_ids) or len(set(user_ids)) != len(user_ids):
            raise ConnectorValidationError("Outlook user_ids must be an explicit array of unique nonempty IDs")
        if not _identity(folder):
            raise ConnectorValidationError("Outlook folder must be a nonempty folder ID or well-known name")
        self.batch_size = batch_size
        self.folder = folder
        self.user_ids = list(user_ids)
        self._check_active = check_active or (lambda: None)
        self.connection: GraphConnection | None = None
        self.scope = hashlib.sha256(json.dumps([self.user_ids, self.folder]).encode()).hexdigest()

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(credentials, dict):
            raise ConnectorMissingCredentialError("Microsoft Graph credentials are incomplete")
        self.connection = GraphConnection(credentials, self._check_active)
        return None

    def _connection(self) -> GraphConnection:
        if self.connection is None:
            raise ConnectorMissingCredentialError("Outlook")
        return self.connection

    def _messages_url(self, user_id: str) -> str:
        return f"{GRAPH_BASE}/users/{quote(user_id, safe='')}/mailFolders/{quote(self.folder, safe='')}/messages"

    def _delta_url(self, user_id: str) -> str:
        return self._messages_url(user_id) + "/delta"

    def _check_link(self, user_id: str, link: str) -> None:
        graph_url(link)
        if urlsplit(link).path != urlsplit(self._delta_url(user_id)).path:
            raise ConnectorValidationError("Outlook continuation changed mailbox or folder scope")

    def validate_connector_settings(self) -> None:
        # Probe Mail.Read, rather than a directory /users identity request.
        for user_id in self.user_ids:
            collection_page(self._connection().json(self._messages_url(user_id) + "?$top=1&$select=id"))
        self._check_active()

    def build_dummy_checkpoint(self) -> OutlookCheckpoint:
        return OutlookCheckpoint(has_more=True, scope=self.scope)

    def validate_checkpoint_json(self, checkpoint_json: str) -> OutlookCheckpoint:
        checkpoint = OutlookCheckpoint.model_validate_json(checkpoint_json, strict=True)
        self._validate_checkpoint(checkpoint)
        return checkpoint

    def _validate_checkpoint(self, checkpoint: OutlookCheckpoint) -> None:
        if checkpoint.scope != self.scope or set(checkpoint.delta_links) - set(self.user_ids):
            raise ConnectorValidationError("Outlook checkpoint scope mismatch")
        for user_id, link in checkpoint.delta_links.items():
            self._check_link(user_id, link)

    def _inventory(self, user_id: str, link: str | None = None) -> tuple[list[dict[str, Any]], str]:
        url: str | None = link or self._delta_url(user_id)
        visited: set[str] = set()
        items: dict[str, dict[str, Any]] = {}
        delta: str | None = None
        while url:
            self._check_active()
            self._check_link(user_id, url)
            if url in visited or len(visited) >= 10_000:
                raise ConnectorValidationError("Outlook pagination did not advance or exceeded page limit")
            visited.add(url)
            page = self._connection().json(url)
            for item in collection_page(page):
                if not _identity(item["id"]):
                    raise ConnectorValidationError("Invalid Outlook message identity")
                if "@removed" in item and not isinstance(item["@removed"], dict):
                    raise ConnectorValidationError("Invalid Outlook removed message")
                # Delta can repeat an ID; the last state, including removal,
                # wins before any document for this mailbox is emitted.
                items[item["id"]] = item
                if len(items) > 100_000:
                    raise ConnectorValidationError("Outlook inventory exceeds item limit")
            url, delta = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
            if url and delta:
                raise ConnectorValidationError("Ambiguous Outlook continuation")
            if delta:
                self._check_link(user_id, delta)
        if not delta:
            raise ConnectorValidationError("Incomplete Outlook delta enumeration")
        self._check_active()
        return list(items.values()), delta

    @staticmethod
    def _time(message: dict[str, Any], field: str) -> datetime:
        raw = message.get(field)
        try:
            if not isinstance(raw, str):
                raise ValueError
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError
            return value.astimezone(UTC)
        except ValueError as exc:
            raise ConnectorValidationError("Invalid Outlook message time") from exc

    @staticmethod
    def _recipient(value: Any) -> str:
        if not isinstance(value, dict) or not isinstance(value.get("emailAddress"), dict):
            raise ConnectorValidationError("Invalid Outlook recipient")
        address = value["emailAddress"]
        if not isinstance(address.get("address"), str) or not address["address"] or not isinstance(address.get("name", ""), str):
            raise ConnectorValidationError("Invalid Outlook recipient address")
        return f"{address.get('name', '')} <{address['address']}>"

    def _document(self, message: dict[str, Any], user_id: str) -> Document:
        body = message.get("body")
        if not isinstance(body, dict) or body.get("contentType") not in {"text", "html"} or not isinstance(body.get("content"), str):
            raise ConnectorValidationError("Invalid Outlook message body")
        subject = message.get("subject")
        if subject is None:
            subject = "(no subject)"
        if not isinstance(subject, str):
            raise ConnectorValidationError("Invalid Outlook subject")
        sender = "" if message.get("from") is None else self._recipient(message["from"])
        recipients: dict[str, list[str]] = {}
        for field in ("toRecipients", "ccRecipients"):
            values = message.get(field, [])
            if not isinstance(values, list) or len(values) > 10_000:
                raise ConnectorValidationError("Invalid Outlook recipients")
            recipients[field] = [self._recipient(value) for value in values]
        if type(message.get("hasAttachments", False)) is not bool:
            raise ConnectorValidationError("Invalid Outlook attachment flag")
        metadata: dict[str, Any] = {
            "user_id": user_id,
            "folder": self.folder,
            "has_attachments": message.get("hasAttachments", False),
            "received_at": self._time(message, "receivedDateTime").isoformat(),
        }
        for field in ("conversationId", "webLink"):
            value = message.get(field, "")
            if not isinstance(value, str):
                raise ConnectorValidationError("Invalid Outlook message metadata")
            metadata[field] = value
        headers = f"From: {sender}\nTo: {', '.join(recipients['toRecipients'])}\nCc: {', '.join(recipients['ccRecipients'])}\nSubject: {subject}"
        content = body["content"] if body["contentType"] == "html" else "<pre>" + html.escape(body["content"]) + "</pre>"
        blob = ("<html><body><pre>" + html.escape(headers) + "</pre>" + content + "</body></html>").encode()
        return Document(
            id=f"{quote(user_id, safe='')}:{quote(message['id'], safe='')}",
            source="outlook",
            semantic_identifier=subject,
            extension=".html",
            blob=blob,
            size_bytes=len(blob),
            doc_updated_at=self._time(message, "lastModifiedDateTime"),
            metadata=metadata,
        )

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, OutlookCheckpoint]:
        if not isinstance(checkpoint, OutlookCheckpoint):
            raise ConnectorValidationError("Outlook checkpoint type mismatch")
        self._validate_checkpoint(checkpoint)
        if isinstance(start, bool) or isinstance(end, bool) or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ConnectorValidationError("Invalid Outlook polling window")
        links = dict(checkpoint.delta_links)
        if checkpoint.has_more:
            for user_id in self.user_ids:
                items, delta = self._inventory(user_id, links.get(user_id))
                for message in items:
                    self._check_active()
                    if "@removed" in message:
                        continue
                    document = self._document(message, user_id)
                    modified = document.doc_updated_at.timestamp()
                    if modified > end:
                        raise ConnectorValidationError("Outlook message time exceeds polling window")
                    if not links.get(user_id) and modified <= start:
                        continue
                    yield document
                links[user_id] = delta
        self._check_active()
        return OutlookCheckpoint(has_more=False, delta_links=links, scope=self.scope)

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Iterator[list[Document]]:
        batch: list[Document] = []
        for document in self.load_from_checkpoint(start, end, self.build_dummy_checkpoint()):
            if not isinstance(document, Document):
                raise ConnectorValidationError("Outlook document failure")
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
    ) -> Generator[Document | ConnectorFailure, None, OutlookCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        # Outlook deletion is deliberately disabled: do not expose a change set
        # as an authoritative snapshot to destructive reconciliation callers.
        raise ConnectorValidationError("Outlook deletion synchronization is not supported")
        yield []
