"""Owned Azure Blob HTTP boundary for real SDK serialization and pagination.

No cloud account, socket, or environment credential is used. The narrow
transport serves listing XML and versioned blob downloads to the installed SDK.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from requests.structures import CaseInsensitiveDict


@dataclass(frozen=True)
class BlobVersion:
    body: bytes
    etag: str = '"version-one"'
    modified: datetime = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SourceRequest:
    method: str
    url: str
    headers: CaseInsensitiveDict[str]


class SourceStream:
    def __init__(self, response: HttpResponse, body: bytes) -> None:
        self.response = response
        self.body = body
        self.content_length = len(body)

    def __iter__(self) -> Iterator[bytes]:
        yield self.body


class SourceResponse(HttpResponse):
    def __init__(self, request: HttpRequest, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.reason = "Controlled Azure response"
        self.headers = CaseInsensitiveDict({"Content-Length": str(len(body)), "Content-Type": "application/octet-stream", "x-ms-request-id": "controlled-request", **(headers or {})})
        self.content_type = self.headers["Content-Type"]
        self._body = body

    def body(self) -> bytes:
        return self._body

    def stream_download(self, pipeline: Any, **kwargs: Any) -> SourceStream:
        return SourceStream(self, self._body)


class BlobSourceTransport(HttpTransport):
    def __init__(self, objects: dict[str, BlobVersion] | None = None, page_size: int = 1) -> None:
        self.objects = objects if objects is not None else {"docs/a.txt": BlobVersion(b"first"), "docs/b.txt": BlobVersion(b"second")}
        self.page_size = page_size
        self.requests: list[SourceRequest] = []
        self.closed = False
        self.close_count = 0
        self.fail_list_page: int | None = None
        self.fail_get: str | None = None
        self.redirect_list = False
        self.race_key: str | None = None
        self.omit_listing_field: str | None = None
        self.list_names: list[str] | None = None
        self.forced_next_marker: str | None = None
        self.error_secret = "synthetic-sas-secret"

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _error(self, request: HttpRequest, status: int, code: str) -> SourceResponse:
        payload = ElementTree.Element("Error")
        ElementTree.SubElement(payload, "Code").text = code
        ElementTree.SubElement(payload, "Message").text = f"controlled failure; sig={self.error_secret}; AccountKey={self.error_secret}"
        return SourceResponse(request, status, ElementTree.tostring(payload), {"Content-Type": "application/xml", "x-ms-error-code": code})

    def _listing(self, request: HttpRequest, query: dict[str, list[str]]) -> SourceResponse:
        if self.redirect_list:
            return SourceResponse(request, 307, headers={"Location": f"https://outside.invalid/container?sig={self.error_secret}"})
        prefix = query.get("prefix", [""])[0]
        offset = int(query.get("marker", ["0"])[0])
        if self.fail_list_page == offset // self.page_size + 1:
            return self._error(request, 403, "AuthorizationPermissionMismatch")
        names = self.list_names if self.list_names is not None else [name for name in self.objects if name.startswith(prefix)]
        selected = names[offset : offset + self.page_size]
        root = ElementTree.Element("EnumerationResults", ServiceEndpoint="https://ownedaccount.blob.core.windows.net/", ContainerName="container")
        ElementTree.SubElement(root, "Prefix").text = prefix
        ElementTree.SubElement(root, "Marker").text = query.get("marker", [""])[0]
        ElementTree.SubElement(root, "MaxResults").text = str(self.page_size)
        blobs = ElementTree.SubElement(root, "Blobs")
        for name in selected:
            version = self.objects[name]
            blob = ElementTree.SubElement(blobs, "Blob")
            ElementTree.SubElement(blob, "Name").text = name
            properties = ElementTree.SubElement(blob, "Properties")
            fields = {
                "Last-Modified": format_datetime(version.modified, usegmt=True),
                "Etag": version.etag,
                "Content-Length": str(len(version.body)),
                "Content-Type": "text/plain",
                "BlobType": "BlockBlob",
                "LeaseStatus": "unlocked",
            }
            for key, value in fields.items():
                if key != self.omit_listing_field:
                    ElementTree.SubElement(properties, key).text = value
        ElementTree.SubElement(root, "NextMarker").text = self.forced_next_marker if self.forced_next_marker is not None else str(offset + len(selected)) if offset + len(selected) < len(names) else ""
        return SourceResponse(request, 200, ElementTree.tostring(root), {"Content-Type": "application/xml"})

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        self.closed = False
        self.requests.append(SourceRequest(request.method, request.url, CaseInsensitiveDict(request.headers)))
        target = urlsplit(request.url)
        assert target.hostname is not None and ".blob." in target.hostname, "SDK followed an untrusted redirect"
        query = parse_qs(target.query)
        if query.get("restype") == ["container"]:
            if request.method == "GET" and "comp" not in query:
                return SourceResponse(request, 200, headers={"ETag": '"container-version"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"})
            assert request.method == "GET" and query.get("comp") == ["list"]
            return self._listing(request, query)
        assert request.method == "GET"
        key = unquote(target.path).removeprefix("/container/")
        if self.fail_get == key:
            return self._error(request, 403, "AuthorizationPermissionMismatch")
        if self.race_key == key:
            self.objects[key] = BlobVersion(b"updated-between-list-and-get", '"version-two"')
            self.race_key = None
        if key not in self.objects:
            return self._error(request, 404, "BlobNotFound")
        version = self.objects[key]
        if request.headers.get("If-Match") != version.etag:
            return self._error(request, 412, "ConditionNotMet")
        if not version.body and request.headers.get("x-ms-range"):
            return self._error(request, 416, "InvalidRange")
        headers = {
            "ETag": version.etag,
            "Last-Modified": format_datetime(version.modified, usegmt=True),
            "x-ms-blob-type": "BlockBlob",
            "Content-Range": f"bytes 0-{len(version.body) - 1}/{len(version.body)}",
        }
        return SourceResponse(request, 206 if version.body else 200, version.body, headers)
