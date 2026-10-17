"""Stateful Azure HTTP boundary; production clients still serialize every request.

No sockets or cloud credentials. This deliberately implements only the Blob/ADLS
operations asserted by the connector contract, not a general Azure emulator.
"""

import inspect
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from azure.storage.blob import ContainerClient
from azure.storage.filedatalake import FileSystemClient
from requests.structures import CaseInsensitiveDict

from core.utils.azure_sas_conn import MultiRAGAzureSasBlob
from core.utils.azure_spn_conn import MultiRAGAzureSpnBlob


class TestCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("controlled-token", 4102444800)


class AzureStream:
    def __init__(self, response: HttpResponse, body: bytes) -> None:
        self.response = response
        self.body = body
        self.content_length = len(body)

    def __iter__(self) -> Iterator[bytes]:
        yield self.body


class AzureResponse(HttpResponse):
    def __init__(self, request: HttpRequest, status: int, body: bytes = b"", **headers: str) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.reason = "Controlled response"
        self.headers = CaseInsensitiveDict(
            {"Content-Length": str(len(body)), "Content-Type": "application/octet-stream", "ETag": '"controlled"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT", **headers}
        )
        self.content_type = self.headers["Content-Type"]
        self._body = body

    def body(self) -> bytes:
        return self._body

    def stream_download(self, pipeline: Any, **kwargs: Any) -> AzureStream:
        return AzureStream(self, self._body)


class AzureTransport(HttpTransport):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.requests: list[tuple[str, str]] = []

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __exit__(self, *args: Any) -> None:
        self.close()

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        url = urlsplit(request.url)
        path = unquote(url.path).split("/", 2)[-1]
        query = parse_qs(url.query)
        self.requests.append((request.method, path))
        headers = {"x-ms-request-id": "controlled-request", "x-ms-version": "2024-08-04"}

        def error(status: int, code: str) -> HttpResponse:
            return AzureResponse(
                request, status, f"<Error><Code>{code}</Code><Message>Controlled error</Message></Error>".encode(), **{**headers, "Content-Type": "application/xml", "x-ms-error-code": code}
            )

        if request.method == "PUT":
            if ".dfs." in (url.hostname or ""):
                parent = path.rpartition("/")[0]
                if parent and parent not in self.directories:
                    return error(404, "PathNotFound")
                if query.get("resource") == ["directory"]:
                    if path in self.directories:
                        assert request.headers.get("If-None-Match") == "*"
                        return error(409, "PathAlreadyExists")
                    self.directories.add(path)
                else:
                    self.objects[path] = b""
            else:
                if path in self.objects and request.headers.get("If-None-Match") == "*":
                    return error(409, "BlobAlreadyExists")
                body = request.body
                self.objects[path] = body.read() if hasattr(body, "read") else bytes(body or b"")
            return AzureResponse(request, 201, **headers)
        if request.method == "PATCH":
            if path not in self.objects:
                return error(404, "PathNotFound")
            if query.get("action") == ["append"]:
                assert query["position"] == [str(len(self.objects[path]))]
                self.objects[path] += bytes(request.body or b"")
                return AzureResponse(request, 202, **headers)
            assert query.get("action") == ["flush"]
            assert query["position"] == [str(len(self.objects[path]))]
            return AzureResponse(request, 200, **headers)
        if path not in self.objects:
            return error(404, "BlobNotFound")
        if request.method == "HEAD":
            return AzureResponse(request, 200, **{**headers, "Content-Length": str(len(self.objects[path])), "x-ms-blob-type": "BlockBlob"})
        if request.method == "GET":
            body = self.objects[path]
            return AzureResponse(request, 206, body, **{**headers, "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}", "x-ms-blob-type": "BlockBlob"})
        assert request.method == "DELETE"
        del self.objects[path]
        return AzureResponse(request, 200 if ".dfs." in (url.hostname or "") else 202, **headers)


def azure_adapter(kind: str, transport: AzureTransport) -> Any:
    """Bypass singleton/config only; inject the documented SDK transport seam."""
    factory = MultiRAGAzureSasBlob if kind == "sas" else MultiRAGAzureSpnBlob
    adapter = object.__new__(inspect.getclosurevars(factory).nonlocals["cls"])
    if kind == "sas":
        adapter.conn = ContainerClient("https://controlled.blob.core.windows.net", "container", credential="sp=rw&sig=controlled", transport=transport, retry_total=0)
    else:
        adapter.conn = FileSystemClient("https://controlled.dfs.core.windows.net", "container", credential=TestCredential(), transport=transport, retry_total=0)
    return adapter
