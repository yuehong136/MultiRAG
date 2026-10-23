"""Real cloud SDK requests over controlled transports; native OpenDAL memory IO."""

import inspect
import json
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urlsplit

import boto3
import opendal
import pytest
import requests
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage as gcs
from urllib3.response import HTTPResponse

from api.db.services.file_service import FileService, RuntimeUploadCleanupError
from common import resources
from core.utils.encrypted_storage import EncryptedStorageWrapper
from core.utils.gcs_conn import MultiRAGGCS
from core.utils.opendal_conn import OpenDALStorage
from core.utils.oss_conn import MultiRAGOSS
from core.utils.s3_conn import MultiRAGS3
from tests.support.azure_transport import AzureResponse, AzureTransport, azure_adapter

OWNER = "cleanup-owner"
NAMESPACE = f"{OWNER}-downloads"
LOCATION = "a" * 32


class WireBody(BytesIO):
    def stream(self, amt: int | None = None, decode_content: bool = False) -> Any:
        yield self.read()


class S3Transport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.fault: str | None = None
        self.bodies: list[WireBody] = []
        self.read_bodies: list[WireBody] = []

    def send(self, request: Any) -> AWSResponse:
        path = unquote(urlsplit(request.url).path).removeprefix("/physical/")
        self.requests.append((request.method, path))
        status, body = 200, b""
        if self.fault == "denied":
            status, body = 403, b"<Error><Code>AccessDenied</Code><Message>controlled</Message></Error>"
        elif request.method == "GET":
            if path in self.objects:
                body = self.objects[path]
            else:
                status, body = 404, b"<Error><Code>NoSuchKey</Code><Message>controlled</Message></Error>"
        else:
            assert request.method == "DELETE"
            status = 204
            if self.fault != "silent":
                self.objects.pop(path, None)
        raw = WireBody(body)
        self.bodies.append(raw)
        if request.method == "GET" and status == 200:
            self.read_bodies.append(raw)
        return AWSResponse(request.url, status, {"content-type": "application/octet-stream", "content-length": str(len(body))}, raw)


class GCSTransport(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.is_mtls = False
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.fault: str | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        path = unquote(urlsplit(url).path.split("/o/", 1)[1])
        self.requests.append((method, path))
        response = requests.Response()
        response.request = requests.Request(method, url).prepare()
        response.url = url
        response.status_code = 200
        body = b""
        if self.fault == "denied":
            response.status_code = 403
        elif method == "GET":
            if path in self.objects:
                body = self.objects[path]
            else:
                response.status_code = 404
        else:
            assert method == "DELETE"
            if path not in self.objects:
                response.status_code = 404
            elif self.fault != "silent":
                self.objects.pop(path)
                response.status_code = 204
        if response.status_code >= 400:
            body = json.dumps({"error": {"code": response.status_code, "message": "controlled"}}).encode()
        response._content = body
        response.headers.update({"Content-Length": str(len(body)), "Content-Type": "application/octet-stream"})
        response.raw = HTTPResponse(body=BytesIO(body), headers=response.headers, status=response.status_code, preload_content=False)
        return response


class StrictAzureTransport(AzureTransport):
    def __init__(self) -> None:
        super().__init__()
        self.fault: str | None = None

    def send(self, request: Any, **kwargs: Any) -> Any:
        if self.fault == "denied":
            return AzureResponse(request, 403, b"<Error><Code>AuthorizationPermissionMismatch</Code><Message>controlled</Message></Error>", **{"Content-Type": "application/xml"})
        if self.fault == "silent" and request.method == "DELETE":
            return AzureResponse(request, 202 if ".blob." in request.url else 200)
        return super().send(request, **kwargs)


def adapter_for(kind: str) -> tuple[Any, Any, str]:
    """Only bypass deployment configuration; all SDK operation paths stay real."""
    if kind in {"sas", "spn"}:
        transport = StrictAzureTransport()
        return azure_adapter(kind, transport), transport, f"{NAMESPACE}/"
    if kind in {"s3", "oss"}:
        transport = S3Transport()
        client = boto3.client(
            "s3",
            endpoint_url="https://controlled.invalid",
            region_name="us-east-1",
            aws_access_key_id="controlled-access",
            aws_secret_access_key="controlled-secret",
            config=Config(retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}),
        )
        client._endpoint.http_session.send = transport.send
        factory = MultiRAGS3 if kind == "s3" else MultiRAGOSS
        adapter = object.__new__(inspect.getclosurevars(factory).nonlocals["cls"])
        adapter.conn = [client] if kind == "s3" else client
        adapter.bucket, adapter.prefix_path = "physical", "prefix"
        return adapter, transport, f"prefix/{NAMESPACE}/" if kind == "s3" else "prefix/"
    transport = GCSTransport()
    adapter = object.__new__(inspect.getclosurevars(MultiRAGGCS).nonlocals["cls"])
    adapter.client = gcs.Client(project="controlled-project", credentials=AnonymousCredentials(), _http=transport)
    adapter.bucket_name = "physical"
    return adapter, transport, f"{NAMESPACE}/"


@pytest.mark.parametrize("kind", ["s3", "oss", "gcs", "sas", "spn"])
@pytest.mark.parametrize("encrypted", [False, True])
def test_cloud_sdk_cleanup_readback_and_idempotency(kind: str, encrypted: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, transport, prefix = adapter_for(kind)
    storage = EncryptedStorageWrapper(adapter, key="0123456789abcdef0123456789abcdef") if encrypted else adapter
    descriptor = {"id": LOCATION, "created_by": OWNER, "name": "empty.txt", "mime_type": "text/plain", "extension": "txt", "size": 0, "created_at": 1.0, "preview_url": None}
    values = {LOCATION: b"", LOCATION + ".upload.json": json.dumps(descriptor).encode(), "unrelated": b"keep"}
    for key, binary in values.items():
        transport.objects[prefix + key] = storage.crypto.encrypt(binary) if encrypted else binary
    monkeypatch.setitem(resources._state, "storage", storage)
    assert storage.get_bytes(NAMESPACE, LOCATION) == b""
    assert storage.get_bytes(NAMESPACE, "missing") is None
    storage.rm(NAMESPACE, "missing")
    FileService.remove_runtime_uploads(OWNER, [LOCATION, LOCATION])
    assert set(transport.objects) == {prefix + "unrelated"}
    assert storage.get_bytes(NAMESPACE, "unrelated") == b"keep"
    assert all(path.startswith(prefix) for _, path in transport.requests)
    # The body streams from successful boto3 reads must be released too.
    if kind in {"s3", "oss"}:
        assert transport.read_bodies and all(body.closed for body in transport.read_bodies)


@pytest.mark.parametrize("kind", ["s3", "oss", "gcs", "sas", "spn"])
@pytest.mark.parametrize("fault", ["denied", "silent"])
def test_cloud_sdk_failures_are_not_absence(kind: str, fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, transport, prefix = adapter_for(kind)
    descriptor = {"id": LOCATION, "created_by": OWNER, "name": "notes.txt", "mime_type": "text/plain", "extension": "txt", "size": 4, "created_at": 1.0, "preview_url": None}
    transport.objects.update({prefix + LOCATION: b"data", prefix + LOCATION + ".upload.json": json.dumps(descriptor).encode()})
    monkeypatch.setitem(resources._state, "storage", adapter)
    transport.fault = fault
    with pytest.raises(RuntimeUploadCleanupError):
        FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert len(transport.objects) == 2
    transport.fault = None
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert transport.objects == {}


def test_opendal_native_operator_and_encryption_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = object.__new__(inspect.getclosurevars(OpenDALStorage).nonlocals["cls"])
    adapter._operator = opendal.Operator("memory")
    storage = EncryptedStorageWrapper(adapter, key="0123456789abcdef0123456789abcdef")
    descriptor = {"id": LOCATION, "created_by": OWNER, "name": "empty.txt", "mime_type": "text/plain", "extension": "txt", "size": 0, "created_at": 1.0, "preview_url": None}
    for key, data in [(LOCATION, b""), (LOCATION + ".upload.json", json.dumps(descriptor).encode()), ("unrelated", b"keep")]:
        storage.put(NAMESPACE, key, data)
    monkeypatch.setitem(resources._state, "storage", storage)
    assert storage.get_bytes(NAMESPACE, LOCATION) == b""
    assert storage.get_bytes(NAMESPACE, "missing") is None
    storage.rm(NAMESPACE, "missing")
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert storage.get_bytes(NAMESPACE, LOCATION) is None and storage.get_bytes(NAMESPACE, LOCATION + ".upload.json") is None
    assert storage.get_bytes(NAMESPACE, "unrelated") == b"keep"
