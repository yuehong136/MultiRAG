"""Real Azure SDK serialization and independent state readback, without Azure.

Only the HTTP transport is controlled. No business objects or shared services
are accessed, and these tests do not prove cloud RBAC, HNS or SAS enforcement.
"""

import pytest
from azure.core.exceptions import ResourceNotFoundError

from core.utils.encrypted_storage import EncryptedStorageWrapper
from tests.support.azure_transport import AzureTransport, azure_adapter


@pytest.mark.parametrize("kind", ["sas", "spn"])
def test_azure_dataset_isolation_and_cleanup(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = AzureTransport()
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
