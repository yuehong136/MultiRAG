"""Salesforce CRM records over OAuth client credentials and paginated SOQL."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from common.data_source.config import INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, IncrementalCapability, LoadConnector, PollConnector, SlimConnectorWithPermSync
from common.data_source.microsoft_graph import _SocketTracker, _TrackedAdapter
from common.data_source.models import ConnectorCheckpoint, ConnectorFailure, Document, SecondsSinceUnixEpoch, SlimDocument

_DEFAULT_OBJECTS = ("Account", "Contact", "Opportunity", "Case", "Knowledge__kav")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_RECORD_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_VERSION = re.compile(r"v[1-9][0-9]*\.[0-9]+")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+salesforce\.com")
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _send_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    """Network replacement seam; controlled tests retain real HTTP preparation."""
    return session.request(method, url, **kwargs)


def _origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ConnectorValidationError("Salesforce instance_url must be a canonical Salesforce HTTPS origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.netloc != host or not _HOST.fullmatch(host) or parsed.path or parsed.query or parsed.fragment or value != f"https://{host}":
            raise ValueError
    except ValueError:
        raise ConnectorValidationError("Salesforce instance_url must be a canonical Salesforce HTTPS origin") from None
    return value


def _timestamp(value: Any) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError:
        raise ConnectorValidationError("Salesforce returned an invalid SystemModstamp") from None


class SalesforceObjectUnavailable(ConnectorValidationError):
    """The explicitly described SObject does not exist."""


class SalesforceCheckpoint(ConnectorCheckpoint):
    cursors: dict[str, str] | None = None


class SalesforceConnector(LoadConnector, PollConnector, CheckpointedConnectorWithPermSync, SlimConnectorWithPermSync):
    INCREMENTAL_CAPABILITY = IncrementalCapability.CURSOR

    def __init__(self, batch_size: int = INDEX_BATCH_SIZE, objects: list[str] | str | None = None, api_version: str = "v59.0", check_active: Callable[[], None] | None = None) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ConnectorValidationError("Salesforce batch_size must be an integer between 1 and 1000")
        if not isinstance(api_version, str) or not _VERSION.fullmatch(api_version):
            raise ConnectorValidationError("Salesforce api_version must have the form v59.0")
        self._optional_objects = {"Knowledge__kav"} if objects is None else set()
        if objects is None:
            selected = list(_DEFAULT_OBJECTS)
        elif isinstance(objects, str):
            selected = objects.split(",") if objects.strip() else []
        elif isinstance(objects, list):
            selected = list(objects)
        else:
            raise ConnectorValidationError("Salesforce objects must be a list or comma-separated API names")
        if any(not isinstance(obj, str) or not _IDENTIFIER.fullmatch(obj.strip()) for obj in selected):
            raise ConnectorValidationError("Salesforce objects must contain valid object API names")
        self.objects = list(dict.fromkeys(obj.strip() for obj in selected))
        self.batch_size, self.api_version = batch_size, api_version
        self.check_active = check_active or (lambda: None)
        self._instance_url: str | None = None
        self._credentials: dict[str, str] | None = None
        self._access_token: str | None = None
        self._session: requests.Session | None = None
        self._unavailable: set[str] = set()

    @classmethod
    def build_connector(cls, config: dict[str, Any], **kwargs: Any) -> SalesforceConnector:
        """Validate saves and worker construction without remote IO."""
        if not isinstance(config, dict):
            raise ConnectorValidationError("Salesforce config must be an object")
        if config.get("sync_deleted_files", False) is not False:
            raise ConnectorValidationError("Salesforce deletion synchronization is not supported")
        connector = cls(batch_size=config.get("batch_size", INDEX_BATCH_SIZE), objects=config.get("objects"), api_version=config.get("api_version", "v59.0"), **kwargs)
        credentials = config.get("credentials")
        if not isinstance(credentials, dict):
            raise ConnectorMissingCredentialError("Salesforce")
        connector.load_credentials(credentials)
        return connector

    def load_credentials(self, credentials: dict[str, Any]) -> None:
        self.close()
        self._credentials = None
        instance = _origin(credentials.get("instance_url"))
        for name in ("client_id", "client_secret"):
            if not isinstance(credentials.get(name), str) or not credentials[name].strip():
                raise ConnectorMissingCredentialError("Salesforce (instance_url, client_id and client_secret are required)")
        self._credentials = {"client_id": credentials["client_id"], "client_secret": credentials["client_secret"]}
        self._instance_url = instance

    def close(self) -> None:
        active_exception = sys.exc_info()[0] is not None
        session, self._session = self._session, None
        self._access_token = None
        self._unavailable = set()
        if session is not None:
            try:
                session.close()
            except Exception:
                if active_exception:
                    logging.warning("Salesforce session close failed")
                else:
                    raise ConnectorValidationError("Salesforce session close failed") from None

    @contextmanager
    def session(self) -> Iterator[None]:
        try:
            yield
        finally:
            self.close()

    def _safe_url(self, value: str) -> str:
        if self._instance_url is None:
            raise ConnectorMissingCredentialError("Salesforce")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ConnectorValidationError("Salesforce request URL is invalid") from None
        if (
            parsed.scheme != "https"
            or f"https://{parsed.netloc}" != self._instance_url
            or parsed.fragment
            or "\\" in value
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or "%" in parsed.path
        ):
            raise ConnectorValidationError("Salesforce request URL is outside the trusted origin")
        return value

    def _request_json(self, method: str, url: str, *, token_request: bool = False, describe: bool = False) -> dict[str, Any]:
        self.check_active()
        url = self._safe_url(url)
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False
        tracker = _SocketTracker()
        self._session.mount("http://", _TrackedAdapter(tracker))
        self._session.mount("https://", _TrackedAdapter(tracker))
        done = threading.Event()
        failures: list[Exception] = []
        deadline = time.monotonic() + 60.0

        def watch() -> None:
            next_check = 0.0
            while not done.wait(0.02):
                try:
                    now = time.monotonic()
                    if now >= deadline:
                        raise TimeoutError("Salesforce request deadline exceeded")
                    if now >= next_check:
                        self.check_active()
                        next_check = now + 0.25
                except Exception as exc:
                    failures.append(exc)
                    tracker.close()
                    return

        watcher = threading.Thread(target=watch, name="salesforce-request-guard")
        watcher.start()
        headers = {"Accept": "application/json"}
        if not token_request:
            headers["Authorization"] = f"Bearer {self._access_token}"
        response: requests.Response | None = None
        try:
            response = _send_request(
                self._session,
                method,
                url,
                headers=headers,
                data={"grant_type": "client_credentials", **(self._credentials or {})} if token_request else None,
                timeout=(5, 30),
                allow_redirects=False,
                stream=True,
            )
            self.check_active()
            if response.status_code != 200:
                if describe and response.status_code == 404:
                    raise SalesforceObjectUnavailable("Salesforce object is unavailable")
                raise ConnectorValidationError(f"Salesforce {'authentication' if token_request else 'request'} failed (HTTP {response.status_code})")
            content = bytearray()
            for chunk in response.iter_content(65536):
                self.check_active()
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise ConnectorValidationError("Salesforce response exceeds the size limit")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ConnectorValidationError("Salesforce returned an invalid response object")
            return data
        except (requests.RequestException, ValueError):
            raise ConnectorValidationError("Salesforce request or response failed; retry synchronization") from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            done.set()
            tracker.close()
            watcher.join()
            http, self._session = self._session, None
            if http is not None:
                try:
                    http.close()
                except Exception:
                    if active_exception:
                        logging.warning("Salesforce session close failed")
                    else:
                        raise ConnectorValidationError("Salesforce session close failed") from None
            if response is not None:
                try:
                    response.close()
                except Exception:
                    if active_exception:
                        logging.warning("Salesforce response close failed")
                    else:
                        raise ConnectorValidationError("Salesforce response close failed") from None
            if failures:
                raise failures[0] from None

    def _authenticate(self) -> None:
        if self._credentials is None or self._instance_url is None:
            raise ConnectorMissingCredentialError("Salesforce")
        if self._access_token is None:
            payload = self._request_json("POST", self._instance_url + "/services/oauth2/token", token_request=True)
            if "instance_url" in payload and _origin(payload["instance_url"]) != self._instance_url:
                raise ConnectorValidationError("Salesforce canonical instance_url differs from the configured origin")
            token = payload.get("access_token")
            if not isinstance(token, str) or not token or any(char.isspace() for char in token):
                raise ConnectorMissingCredentialError("Salesforce (invalid access token response)")
            self._access_token = token

    def _base(self) -> str:
        return f"{self._instance_url}/services/data/{self.api_version}"

    def _validate_remote(self) -> None:
        self._authenticate()
        payload = self._request_json("GET", self._base() + "/sobjects")
        entries = payload.get("sobjects")
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("queryable")) is not bool for item in entries):
            raise ConnectorValidationError("Salesforce returned an invalid object inventory")
        available = {item["name"]: item["queryable"] for item in entries}
        for obj in self.objects:
            if obj not in available and obj in self._optional_objects:
                self._unavailable.add(obj)
            elif available.get(obj) is not True:
                raise ConnectorValidationError("Salesforce configured object is unavailable or not queryable")

    def validate_connector_settings(self) -> None:
        with self.session():
            if self.objects:
                self._validate_remote()

    def _describe_fields(self, obj: str) -> list[str]:
        payload = self._request_json("GET", f"{self._base()}/sobjects/{obj}/describe", describe=True)
        raw = payload.get("fields")
        if not isinstance(raw, list) or any(not isinstance(field, dict) or not isinstance(field.get("name"), str) or not _IDENTIFIER.fullmatch(field["name"]) for field in raw):
            raise ConnectorValidationError("Salesforce returned invalid field API names")
        fields = list(dict.fromkeys(field["name"] for field in raw if field.get("type") not in {"address", "location"}))
        if not {"Id", "SystemModstamp"}.issubset(fields):
            raise ConnectorValidationError("Salesforce object must expose Id and SystemModstamp")
        return fields

    def _query_records(self, obj: str, fields: list[str], start: float | None, end: float | None) -> Iterator[dict[str, Any]]:
        instance_url = self._instance_url
        if instance_url is None:
            raise ConnectorMissingCredentialError("Salesforce")
        filters = []
        wire_start = math.floor(start) if start is not None else None
        wire_end = math.ceil(end) if end is not None else None
        for value, operator in ((wire_start, ">="), (wire_end, "<=")):
            if value is not None:
                if not math.isfinite(value):
                    raise ConnectorValidationError("Salesforce poll bounds must be finite")
                stamp = datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                filters.append(f"SystemModstamp {operator} {stamp}")
        where = " WHERE " + " AND ".join(filters) if filters else ""
        query = f"SELECT {','.join(fields)} FROM {obj}{where} ORDER BY SystemModstamp ASC, Id ASC"
        url = self._base() + "/query?q=" + quote(query, safe="")
        visited: set[str] = set()
        ids: set[str] = set()
        total: int | None = None
        previous: datetime | None = None
        while True:
            self.check_active()
            if url in visited or len(visited) >= 100000:
                raise ConnectorValidationError("Salesforce pagination repeated or exceeded the page limit")
            visited.add(url)
            page = self._request_json("GET", url)
            records, done, count = page.get("records"), page.get("done"), page.get("totalSize")
            if not isinstance(records, list) or type(done) is not bool or type(count) is not int or count < 0 or (total is not None and count != total):
                raise ConnectorValidationError("Salesforce returned an incomplete query page")
            total = count
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("Id"), str) or not _RECORD_ID.fullmatch(record["Id"]) or record["Id"] in ids:
                    raise ConnectorValidationError("Salesforce returned an invalid or duplicate record ID")
                modified = _timestamp(record.get("SystemModstamp"))
                if (previous is not None and modified < previous) or (wire_start is not None and modified.timestamp() < wire_start) or (wire_end is not None and modified.timestamp() > wire_end):
                    raise ConnectorValidationError("Salesforce returned an out-of-window record version")
                ids.add(record["Id"])
                previous = modified
                # SOQL accepts whole seconds. Count the complete rounded query
                # inventory, but only publish records in the precise window.
                if (start is None or modified.timestamp() >= start) and (end is None or modified.timestamp() <= end):
                    yield record
            next_url = page.get("nextRecordsUrl")
            if done:
                if next_url or len(ids) != total:
                    raise ConnectorValidationError("Salesforce query did not return its complete inventory")
                return
            if not isinstance(next_url, str) or not next_url or not records:
                raise ConnectorValidationError("Salesforce query is missing its continuation")
            try:
                url = self._safe_url(urljoin(instance_url + "/", next_url))
                parsed = urlsplit(url)
            except ValueError:
                raise ConnectorValidationError("Salesforce continuation URL is invalid") from None
            if not parsed.path.startswith(f"/services/data/{self.api_version}/query/") or parsed.query:
                raise ConnectorValidationError("Salesforce continuation is outside the query endpoint")

    @staticmethod
    def _record_to_text(obj: str, record: dict[str, Any]) -> str:
        lines = [f"Salesforce {obj}"]
        for key in sorted(record):
            value = record[key]
            if key != "attributes" and value is not None and value != "":
                rendered = json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)

    def _iter_documents(self, start: float | None = None, end: float | None = None, checkpoint: SalesforceCheckpoint | None = None) -> Generator[list[Document], None, None]:
        with self.session():
            if any(value is not None and not math.isfinite(value) for value in (start, end)) or (start is not None and end is not None and end < start):
                raise ConnectorValidationError("Salesforce poll window must be finite and ordered")
            if self.objects:
                self._validate_remote()
            cursors = dict(checkpoint.cursors or {}) if checkpoint else {}
            batch: list[Document] = []
            for obj in self.objects:
                if obj in self._unavailable:
                    continue
                # A successful inventory already identified absent defaults.
                # A later describe failure is incomplete access, not absence.
                fields = self._describe_fields(obj)
                lower = max(start, _timestamp(cursors[obj]).timestamp()) if start is not None and obj in cursors else _timestamp(cursors[obj]).timestamp() if obj in cursors else start
                for record in self._query_records(obj, fields, lower, end):
                    modified = _timestamp(record["SystemModstamp"])
                    blob = self._record_to_text(obj, record).encode()
                    batch.append(
                        Document(
                            id=f"{obj}/{record['Id']}",
                            source=DocumentSource.SALESFORCE,
                            semantic_identifier=str(record.get("Name") or record.get("Subject") or record.get("Title") or f"{obj}/{record['Id']}"),
                            extension=".txt",
                            blob=blob,
                            size_bytes=len(blob),
                            doc_updated_at=modified,
                            metadata={"object": obj, "record_id": record["Id"], "web_url": f"{self._instance_url}/{record['Id']}"},
                        )
                    )
                    cursors[obj] = modified.isoformat()
                    if len(batch) >= self.batch_size:
                        yield batch
                        batch = []
            if batch:
                yield batch
            if checkpoint is not None:
                checkpoint.cursors, checkpoint.has_more = cursors, False

    def load_from_state(self) -> Generator[list[Document], None, None]:
        return self._iter_documents()

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        return self._iter_documents(start, end)

    def build_dummy_checkpoint(self) -> SalesforceCheckpoint:
        return SalesforceCheckpoint(cursors={})

    def validate_checkpoint_json(self, checkpoint_json: str) -> SalesforceCheckpoint:
        checkpoint = SalesforceCheckpoint.model_validate_json(checkpoint_json)
        if checkpoint.cursors:
            for obj, stamp in checkpoint.cursors.items():
                if obj not in self.objects:
                    raise ConnectorValidationError("Salesforce checkpoint contains an out-of-scope object")
                _timestamp(stamp)
        return checkpoint

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        if not isinstance(checkpoint, SalesforceCheckpoint):
            raise ConnectorValidationError("Salesforce checkpoint has the wrong type")
        for batch in self._iter_documents(start, end, checkpoint):
            yield from batch
        return checkpoint

    def load_from_checkpoint_with_perm_sync(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint
    ) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        # This interface is useful for callers, but does not authorize prune.
        batch: list[SlimDocument] = []
        with self.session():
            if self.objects:
                self._validate_remote()
            for obj in self.objects:
                if obj in self._unavailable:
                    continue
                for record in self._query_records(obj, ["Id", "SystemModstamp"], None, None):
                    identity = f"{obj}/{record['Id']}"
                    if callback:
                        callback(identity, obj)
                    batch.append(SlimDocument(id=identity))
                    if len(batch) >= self.batch_size:
                        yield batch
                        batch = []
            if batch:
                yield batch
