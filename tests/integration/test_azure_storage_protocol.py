"""Real Azure SDK serialization and independent state readback, without Azure.

Only the HTTP transport is controlled. No business objects or shared services
are accessed, and these tests do not prove cloud RBAC, HNS or SAS enforcement.
"""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from azure.core import PipelineClient
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.core.pipeline.transport import HttpRequest
from azure.storage.filedatalake import DataLakeServiceClient

from core.utils.encrypted_storage import EncryptedStorageWrapper
from tests.support.azure_transport import AzureTransport, azure_adapter


@pytest.mark.parametrize(("kind", "existing_directory_status"), [("sas", 409), ("spn", 409), ("spn", 412)])
def test_azure_dataset_isolation_and_cleanup(kind: str, existing_directory_status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = AzureTransport()
    transport.existing_directory_status = existing_directory_status
    adapter = azure_adapter(kind, transport)
    monkeypatch.setattr(adapter, "__open__", lambda: None)
    monkeypatch.setattr(f"core.utils.azure_{kind}_conn.time.sleep", lambda _: None)
    filename = "folder/同名 #?.txt"
    # Legacy data must never be a fallback for an unowned dataset lookup.
    transport.objects[filename] = b"unassigned legacy data"
    adapter.put("dataset-a", filename, b"first", "tenant")
    adapter.put("dataset-b", filename, b"second", "tenant")
    assert transport.objects[f"dataset-a/{filename}"] == b"first"
    assert transport.objects[f"dataset-b/{filename}"] == b"second"
    assert adapter.get("dataset-a", filename, "tenant") == b"first"
    assert adapter.get("dataset-b", filename, "tenant") == b"second"
    assert adapter.obj_exist("dataset-a", filename, "tenant") is True
    assert adapter.obj_exist("dataset-c", filename, "tenant") is False
    with pytest.raises(ResourceNotFoundError):
        adapter.get("dataset-c", filename)
    adapter.put("dataset-a", filename, b"updated")
    assert transport.objects[f"dataset-a/{filename}"] == b"updated"
    assert adapter.get("dataset-b", filename) == b"second"
    adapter.rm("dataset-a", filename, "tenant")
    assert f"dataset-a/{filename}" not in transport.objects
    assert adapter.obj_exist("dataset-a", filename) is False
    assert adapter.get("dataset-b", filename) == b"second"
    assert transport.objects[filename] == b"unassigned legacy data"
    adapter.health()
    adapter.health()
    assert transport.objects["txtxtxtxt1/txtxtxtxt1"] == b"_t@@@1"
    adapter.rm("dataset-b", filename)
    adapter.rm("txtxtxtxt1", "txtxtxtxt1")
    assert transport.objects == {filename: b"unassigned legacy data"}
    # All non-fixture HTTP requests include the logical bucket, including misses.
    assert all(path.startswith(("dataset-", "txtxtxtxt1")) for _, path in transport.requests)


@pytest.mark.parametrize("kind", ["sas", "spn"])
def test_azure_encrypted_wrapper_contract(kind: str) -> None:
    transport = AzureTransport()
    adapter = azure_adapter(kind, transport)
    storage = EncryptedStorageWrapper(adapter, key="0123456789abcdef0123456789abcdef")
    storage.put("dataset", "file", b"payload", "tenant")
    assert transport.objects["dataset/file"] != b"payload"
    assert storage.get("dataset", "file", "tenant") == b"payload"
    assert storage.obj_exist("dataset", "file", "tenant")
    storage.rm("dataset", "file", "tenant")
    assert transport.objects == {}


def test_spn_object_scoped_presigned_url(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = AzureTransport()
    adapter = azure_adapter("spn", transport)
    # Keep the real SDK/key exchange; supply only our owned HTTP transport.
    monkeypatch.setattr("core.utils.azure_spn_conn.DataLakeServiceClient", partial(DataLakeServiceClient, transport=transport, retry_total=0))
    filename = "nested/同名 #?.txt"
    adapter.put("dataset-a", filename, b"download-a")
    adapter.put("dataset-b", filename, b"download-b")
    before = datetime.now(UTC).replace(microsecond=0)
    url = adapter.get_presigned_url("dataset-a", filename, 3600, "tenant")
    after = datetime.now(UTC)
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    assert path == f"/container/dataset-a/{filename}"
    assert parsed.scheme == "https" and parsed.hostname == "controlled.dfs.core.windows.net"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert query["sr"] == "b" and query["sp"] == "r" and query["spr"] == "https"
    expiry = datetime.fromisoformat(query["se"].replace("Z", "+00:00"))
    assert before + timedelta(seconds=3600) <= expiry <= after + timedelta(seconds=3600)
    assert len(transport.delegation_windows) == 1
    assert transport.delegation_windows[0] == (query["skt"], query["ske"])
    assert query["ske"] == query["se"]
    assert transport.closed
    # Independent HMAC from the documented Azure user-delegation string-to-sign.
    # This binds the signed object path; changing only the dataset cannot reuse it.
    fields = [query.get(k, "") for k in ("sp", "st", "se")]
    fields += [f"/blob/controlled{path}"]
    fields += [query.get(k, "") for k in ("skoid", "sktid", "skt", "ske", "sks", "skv", "saoid", "suoid", "scid", "sip", "spr", "sv", "sr", "snapshot", "ses", "rscc", "rscd", "rsce", "rscl", "rsct")]
    signed = "\n".join(fields).encode()
    key = base64.b64decode(transport.delegation_key)
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert hmac.compare_digest(query["sig"], expected)
    changed = signed.replace(b"dataset-a/", b"dataset-b/")
    assert not hmac.compare_digest(query["sig"], base64.b64encode(hmac.new(key, changed, hashlib.sha256).digest()).decode())
    with PipelineClient(base_url=adapter.account_url, transport=transport) as consumer:
        assert consumer.send_request(HttpRequest("GET", url), stream=True).body() == b"download-a"
    assert adapter.get("dataset-a", filename) == b"download-a"
    assert adapter.get("dataset-b", filename) == b"download-b"
    adapter.rm("dataset-a", filename)
    adapter.rm("dataset-b", filename)
    assert transport.objects == {}


def test_spn_delegation_denied_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = AzureTransport()
    transport.fail_delegation = True
    adapter = azure_adapter("spn", transport)
    monkeypatch.setattr("core.utils.azure_spn_conn.DataLakeServiceClient", partial(DataLakeServiceClient, transport=transport, retry_total=0))
    with pytest.raises(HttpResponseError) as error:
        adapter.get_presigned_url("dataset", "file", 3600)
    assert error.value.status_code == 403
    assert transport.closed
    assert transport.objects == {}
    assert transport.requests == [("POST", "")]
