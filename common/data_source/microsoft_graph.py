"""Bounded, lazy app-only Microsoft Graph reads for Teams and OneDrive."""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

import msal
import requests
from office365.graph_client import GraphClient
from office365.graph_request import GraphRequest
from office365.runtime.client_object_collection import ClientObjectCollection
from office365.runtime.http.request_options import RequestOptions
from office365.runtime.queries.client_query import ClientQuery
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError as UrllibHTTPError

from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
MAX_RESPONSE_BYTES = 100 * 1024 * 1024


def graph_url(url: str) -> str:
    target = urlsplit(url)
    if target.scheme != "https" or target.netloc != "graph.microsoft.com" or target.fragment or not target.path.startswith("/v1.0/"):
        raise ConnectorValidationError("Unexpected Microsoft Graph URL")
    if any(part in {".", ".."} for part in unquote(target.path).split("/")):
        raise ConnectorValidationError("Unexpected Microsoft Graph path")
    return url


def graph_batch_size(config: dict[str, Any]) -> int:
    for key in ("batch_size", "sync_batch_size"):
        if key in config and (type(config[key]) is not int or config[key] <= 0):
            raise ConnectorValidationError("Microsoft Graph batch sizes must be positive integers")
    return config.get("sync_batch_size", config.get("batch_size", INDEX_BATCH_SIZE))


def _send_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    """The only network replacement boundary for controlled tests."""
    return session.request(method, url, **kwargs)


class _SocketTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connections: list[Any] = []
        self.sockets: list[socket.socket] = []
        self.closed = False

    def add_connection(self, connection: Any) -> None:
        with self.lock:
            self.connections.append(connection)

    def add_socket(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.append(sock)
            closed = self.closed
        if closed:
            sock.close()
            raise TimeoutError("Microsoft Graph request already stopped")

    def close(self) -> None:
        with self.lock:
            self.closed = True
            sockets = [*self.sockets, *(getattr(conn, "sock", None) for conn in self.connections)]
        for sock in sockets:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


def _tracked_connection(base: Any, tracker: _SocketTracker) -> type[Any]:
    class Connection(base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            tracker.add_connection(self)

        def _new_conn(self) -> socket.socket:
            sock = super()._new_conn()
            tracker.add_socket(sock)
            return sock

    return Connection


class _TrackedAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, tracker: _SocketTracker) -> None:
        self.tracker = tracker
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        # Per-session classes: do not mutate urllib3's shared global pool map.
        self.poolmanager.pool_classes_by_scheme = {
            "http": type("TrackedHTTPPool", (HTTPConnectionPool,), {"ConnectionCls": _tracked_connection(HTTPConnection, self.tracker)}),
            "https": type("TrackedHTTPSPool", (HTTPSConnectionPool,), {"ConnectionCls": _tracked_connection(HTTPSConnection, self.tracker)}),
        }


class BoundedHTTPClient:
    """MSAL-compatible HTTP client; guard closure interrupts headers and bodies.

    Network IO remains in the calling thread. The watchdog owns no network IO,
    closes only this request's sockets and is joined before the call returns.
    """

    def __init__(self, connection: GraphConnection) -> None:
        self.connection = connection

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        check_active = self.connection.check_active
        remaining = self.connection.remaining()
        requested = kwargs.get("timeout")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool) and requested > 0:
            remaining = min(remaining, requested)
        deadline = time.monotonic() + min(60.0, remaining)
        tracker = _SocketTracker()
        done = threading.Event()
        failure: list[Exception] = []
        session = requests.Session()
        session.trust_env = False
        adapter = _TrackedAdapter(tracker)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        def watch() -> None:
            next_cancel_check = 0.0
            while not done.wait(0.02):
                try:
                    now = time.monotonic()
                    if now >= deadline:
                        raise TimeoutError("Microsoft Graph transport deadline exceeded")
                    # The worker guard queries SQL. Poll cancellation at most
                    # four times/second while retaining a fine deadline clock.
                    if now >= next_cancel_check:
                        check_active()
                        next_cancel_check = now + 0.25
                except Exception as exc:
                    failure.append(exc)
                    tracker.close()
                    return

        watcher = threading.Thread(target=watch, name="microsoft-graph-request-guard")
        watcher.start()
        try:
            kwargs.update(timeout=(min(10.0, remaining), min(60.0, remaining)), stream=True, allow_redirects=False)
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "Accept-Encoding": "identity"}
            response = _send_request(session, method, url, **kwargs)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in self.connection._chunks(response):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ConnectorValidationError("Microsoft Graph response exceeds size limit")
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response.__dict__["_content_consumed"] = True
                check_active()
                return response
            finally:
                response.close()
        except (requests.RequestException, UrllibHTTPError, OSError):
            if failure:
                raise failure[0] from None
            check_active()
            raise ConnectorValidationError("Microsoft Graph transport failed") from None
        finally:
            done.set()
            tracker.close()
            session.close()
            watcher.join()
            if failure:
                raise failure[0] from None

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)


class GraphConnection:
    def __init__(self, credentials: dict[str, Any], check_active: Callable[[], None]) -> None:
        if any(not isinstance(credentials.get(key), str) or not credentials[key].strip() for key in ("tenant_id", "client_id", "client_secret")):
            raise ConnectorMissingCredentialError("Microsoft Graph credentials are incomplete")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", credentials["tenant_id"]):
            raise ConnectorValidationError("Invalid Microsoft Graph tenant_id")
        self.credentials = dict(credentials)
        self.check_active = check_active
        self.app: Any = None
        self.http = BoundedHTTPClient(self)

    def token(self) -> dict[str, Any]:
        self.check_active()
        if self.app is None:
            self.app = msal.ConfidentialClientApplication(
                client_id=self.credentials["client_id"],
                client_credential=self.credentials["client_secret"],
                authority=f"https://login.microsoftonline.com/{self.credentials['tenant_id']}",
                timeout=min(30.0, self.remaining()),
                http_client=self.http,
            )
        result = self.app.acquire_token_for_client(scopes=GRAPH_SCOPES)
        self.check_active()
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise ConnectorMissingCredentialError("Failed to acquire Microsoft Graph access token")
        return result

    def remaining(self) -> float:
        self.check_active()
        return getattr(self.check_active, "remaining_seconds", lambda: 60.0)()

    def _chunks(self, response: requests.Response) -> Iterator[bytes]:
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ConnectorValidationError("Unexpected Microsoft Graph content encoding")
        if response.__dict__.get("_content_consumed"):
            # Controlled HTTP fixtures already hold complete bytes. Real streamed
            # responses use raw.read1: iter_content/read can wait to fill 64 KiB.
            for offset in range(0, len(response.content), 64 * 1024):
                self.check_active()
                yield response.content[offset : offset + 64 * 1024]
            return
        raw = response.raw
        if not callable(getattr(raw, "read1", None)):
            raise ConnectorValidationError("Unsupported Microsoft Graph streaming transport")
        while not raw.closed and getattr(raw, "length_remaining", None) != 0:
            remaining = self.remaining()
            fp = getattr(getattr(raw, "_fp", None), "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
            if sock is None:
                raise ConnectorValidationError("Missing Microsoft Graph streaming socket")
            sock.settimeout(min(10.0, remaining))
            try:
                chunk = raw.read1(64 * 1024, decode_content=False)
            except (UrllibHTTPError, OSError):
                self.check_active()
                raise ConnectorValidationError("Microsoft Graph content transport failed") from None
            self.check_active()
            if not chunk:
                break
            yield chunk

    def read(self, url: str, headers: dict[str, str] | None = None, *, content: bool = False) -> requests.Response:
        graph_url(url)  # Validate before acquiring or sending any bearer token.
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token()["access_token"]}
        headers = {**headers, "Accept-Encoding": "identity"}
        for redirects in range(4):
            self.check_active()
            remaining = self.remaining()
            try:
                response = self.http.get(url, headers=headers, timeout=(min(10.0, remaining), min(60.0, remaining)), allow_redirects=False, stream=True)
            except requests.RequestException:
                raise ConnectorValidationError("Microsoft Graph transport failed") from None
            try:
                self.check_active()
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urlsplit(response.headers.get("Location", ""))
                    allowed = target.hostname and any(target.hostname.endswith("." + suffix) for suffix in ("sharepoint.com", "1drv.com", "onedrive.com", "microsoft.com"))
                    if not content or redirects == 3 or target.scheme != "https" or target.username or target.password or target.port not in (None, 443) or not allowed:
                        raise ConnectorValidationError("Unexpected Microsoft Graph redirect")
                    url, headers = response.headers["Location"], {"Accept-Encoding": "identity"}  # Signed downloads never receive the Graph bearer.
                    continue
                if response.status_code >= 400:
                    raise ConnectorValidationError(f"Microsoft Graph request failed (HTTP {response.status_code})")
                if response.status_code != 200:
                    raise ConnectorValidationError("Unexpected Microsoft Graph response status")
                self.check_active()
                return response
            finally:
                response.close()
        raise ConnectorValidationError("Too many Microsoft Graph redirects")

    def json(self, url: str) -> dict[str, Any]:
        response = self.read(url)
        try:
            value = response.json()
        except ValueError as exc:
            raise ConnectorValidationError("Invalid Microsoft Graph JSON") from exc
        if not isinstance(value, dict):
            raise ConnectorValidationError("Invalid Microsoft Graph object")
        return value


def collection_page(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise ConnectorValidationError("Invalid Microsoft Graph collection")
    items = payload["value"]
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in items):
        raise ConnectorValidationError("Invalid Microsoft Graph collection item")
    for field in ("@odata.nextLink", "@odata.deltaLink"):
        if field in payload:
            if not isinstance(payload[field], str) or not payload[field]:
                raise ConnectorValidationError("Invalid Microsoft Graph continuation")
            graph_url(payload[field])
    return items


class _GraphRequest(GraphRequest):
    def __init__(self, connection: GraphConnection, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.connection = connection

    def execute_request_direct(self, request: RequestOptions) -> requests.Response:
        if request.method != "GET":
            raise ConnectorValidationError("Unexpected Microsoft Graph mutation")
        graph_url(request.url)
        self.beforeExecute.notify(request)
        return self.connection.read(request.url, dict(request.headers))

    def process_response(self, response: requests.Response, query: ClientQuery) -> None:
        if isinstance(query.return_type, ClientObjectCollection):
            for item in collection_page(response.json()):
                if "body" in item:
                    body = item["body"]
                    if not isinstance(body, dict) or not isinstance(body.get("content"), str) or body.get("contentType") not in {"text", "html"}:
                        raise ConnectorValidationError("Invalid Teams message body")
                    if "attachments" in item and (not isinstance(item["attachments"], list) or any(not isinstance(value, dict) for value in item["attachments"])):
                        raise ConnectorValidationError("Invalid Teams attachments")
                    raw = item.get("lastModifiedDateTime") or item.get("createdDateTime")
                    try:
                        if not isinstance(raw, str) or datetime.fromisoformat(raw.replace("Z", "+00:00")).tzinfo is None:
                            raise ValueError
                    except ValueError as exc:
                        raise ConnectorValidationError("Invalid Teams message time") from exc
        super().process_response(response, query)


class BoundedGraphClient(GraphClient):
    def __init__(self, connection: GraphConnection) -> None:
        super().__init__(connection.token)
        self.connection = connection
        self._bounded_request: GraphRequest | None = None

    def pending_request(self) -> GraphRequest:
        if self._bounded_request is None:
            request = _GraphRequest(self.connection, tenant=self._tenant, environment=self._environment)
            request.with_access_token(self._token_callback)
            request.beforeExecute += self._build_specific_query
            self._bounded_request = request
        return self._bounded_request


def iter_collection(collection: Any, batch_size: int, check_active: Callable[[], None]) -> Iterator[Any]:
    next_links: set[str] = set()
    identifiers: set[str] = set()

    def page_loaded(page: Any) -> None:
        check_active()
        link = page._next_request_url
        if link is not None:
            if link in next_links:
                raise ConnectorValidationError("Microsoft Graph pagination did not advance")
            next_links.add(link)
            if len(next_links) > 10_000:
                raise ConnectorValidationError("Microsoft Graph page limit exceeded")

    loaded = collection.paged(batch_size, page_loaded).get().execute_query()
    check_active()
    for item in loaded:
        check_active()
        if not isinstance(item.id, str) or not item.id or item.id in identifiers:
            raise ConnectorValidationError("Duplicate or missing Microsoft Graph identity")
        identifiers.add(item.id)
        if len(identifiers) > 100_000:
            raise ConnectorValidationError("Microsoft Graph collection limit exceeded")
        yield item
    check_active()
