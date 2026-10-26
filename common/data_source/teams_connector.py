"""Import Teams channel conversations using lazy app-only Microsoft Graph auth.

Team.ReadBasic.All and ChannelMessage.Read.All require admin consent. Dataset
access remains local; source ACL visibility does not authorize deletion sync.
"""

import math
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from html import escape
from typing import Any

from office365.runtime.client_value_collection import ClientValueCollection
from pydantic import ConfigDict

from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, SecondsSinceUnixEpoch, SlimConnectorWithPermSync
from common.data_source.microsoft_graph import MAX_RESPONSE_BYTES, BoundedGraphClient, GraphConnection, iter_collection
from common.data_source.models import ConnectorCheckpoint, ConnectorFailure, Document, SlimDocument


class TeamsCheckpoint(ConnectorCheckpoint):
    """A complete traversal finishes one checkpoint; errors never finish it."""

    model_config = ConfigDict(extra="forbid")


class TeamsConnector(CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ConnectorValidationError("Teams batch_size must be a positive integer")
        self.batch_size = batch_size
        self._check_active = check_active or (lambda: None)
        self.graph_client: BoundedGraphClient | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.graph_client = BoundedGraphClient(GraphConnection(credentials, self._check_active))
        return None

    def _teams(self) -> Iterator[Any]:
        if self.graph_client is None:
            raise ConnectorMissingCredentialError("Microsoft Teams")
        yield from iter_collection(self.graph_client.teams, self.batch_size, self._check_active)

    def validate_connector_settings(self) -> None:
        # A successful explicit empty collection is a valid accessible tenant.
        for _team in self._teams():
            pass

    @staticmethod
    def _prop(obj: Any, name: str) -> Any:
        return getattr(obj, "properties", {}).get(name)

    @classmethod
    def _modified(cls, message: Any) -> datetime:
        value = cls._prop(message, "lastModifiedDateTime") or cls._prop(message, "createdDateTime")
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ConnectorValidationError("Invalid Teams message time") from exc
            if value.tzinfo is None:
                raise ConnectorValidationError("Teams message time has no timezone")
        if not isinstance(value, datetime):
            raise ConnectorValidationError("Missing Teams message time")
        # The pinned SDK converts Graph's Z timestamps to naive datetime.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _body(cls, message: Any) -> tuple[str, str]:
        body = cls._prop(message, "body")
        if not isinstance(body, dict):
            body = {"content": getattr(body, "content", None), "contentType": getattr(body, "contentType", None)}
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or body.get("contentType", "text").lower() not in {"text", "html"}:
            raise ConnectorValidationError("Invalid Teams message body")
        return body["content"], body.get("contentType", "text").lower()

    def _threads(self) -> Iterator[tuple[Any, Any, Any]]:
        for team in self._teams():
            for channel in iter_collection(team.channels, self.batch_size, self._check_active):
                for message in iter_collection(channel.messages, self.batch_size, self._check_active):
                    yield team, channel, message
        self._check_active()

    def _document(self, team: Any, channel: Any, message: Any, replies: list[Any]) -> Document:
        thread = [message, *replies]
        bodies = [self._body(item) for item in thread]
        html = any(kind == "html" for _, kind in bodies)
        parts = [text if kind == "html" or not html else "<pre>" + escape(text) + "</pre>" for text, kind in bodies]
        # Reference attachments are preserved as links, not fetched with Graph
        # bearer credentials. Inline adaptive-card JSON stays as textual content.
        for item in thread:
            attachments = self._prop(item, "attachments") or []
            if not isinstance(attachments, (list, ClientValueCollection)):
                raise ConnectorValidationError("Invalid Teams attachments")
            for attachment in attachments:
                data = attachment if isinstance(attachment, dict) else vars(attachment)
                for field in ("name", "contentUrl", "content"):
                    text = data.get(field)
                    if text is not None:
                        if not isinstance(text, str):
                            raise ConnectorValidationError("Invalid Teams attachment content")
                        parts.append("<pre>" + escape(text) + "</pre>" if html else text)
        body = ("\n\n".join(parts)).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ConnectorValidationError("Teams thread exceeds size limit")
        channel_name = self._prop(channel, "displayName") or channel.id
        metadata = {"team": self._prop(team, "displayName") or team.id, "team_id": team.id, "channel": channel_name, "channel_id": channel.id}
        url = self._prop(message, "webUrl")
        if url:
            metadata["web_url"] = url
        return Document(
            id=f"{team.id}__{channel.id}__{message.id}",
            source="teams",
            semantic_identifier=f"{channel_name}: {message.id}",
            extension=".html" if html else ".txt",
            blob=body,
            size_bytes=len(body),
            doc_updated_at=max(self._modified(item) for item in thread),
            metadata=metadata,
        )

    def _generate_documents(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Iterator[Document]:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ConnectorValidationError("Invalid Teams polling window")
        for team, channel, message in self._threads():
            # Fetch replies even when the parent is old: Graph does not promise
            # that adding a reply changes the parent's lastModifiedDateTime.
            replies = list(iter_collection(message.replies, self.batch_size, self._check_active))
            document = self._document(team, channel, message, replies)
            if start < document.doc_updated_at.timestamp() <= end:
                self._check_active()
                yield document
        self._check_active()

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

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        if checkpoint.has_more:
            yield from self._generate_documents(start, end)
        self._check_active()
        return TeamsCheckpoint(has_more=False)

    def load_from_checkpoint_with_perm_sync(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint
    ) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def build_dummy_checkpoint(self) -> TeamsCheckpoint:
        return TeamsCheckpoint(has_more=True)

    def validate_checkpoint_json(self, checkpoint_json: str) -> TeamsCheckpoint:
        return TeamsCheckpoint.model_validate_json(checkpoint_json, strict=True)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        batch: list[SlimDocument] = []
        for team, channel, message in self._threads():
            if callback is not None and callback.should_stop():
                raise RuntimeError("Teams enumeration cancelled")
            batch.append(SlimDocument(id=f"{team.id}__{channel.id}__{message.id}"))
            if len(batch) >= self.batch_size:
                self._check_active()
                yield batch
                batch = []
        self._check_active()
        if batch:
            yield batch
