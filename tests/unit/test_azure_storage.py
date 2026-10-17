"""Azure connector failure semantics, with SDK methods checked by mock specs."""

from typing import Any
from unittest.mock import Mock

import pytest

from tests.support.azure_transport import AzureTransport, azure_adapter


@pytest.mark.parametrize("kind", ["sas", "spn"])
@pytest.mark.parametrize("operation", ["put", "get"])
def test_azure_retries_then_raises(kind: str, operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = azure_adapter(kind, AzureTransport())
    conn = Mock(spec=type(adapter.conn))
    adapter.conn = conn
    call = conn.upload_blob if kind == "sas" else conn.create_file
    if operation == "get":
        call = conn.download_blob if kind == "sas" else conn.get_file_client
    call.side_effect = OSError("controlled transport failure")
    reopen = Mock()
    monkeypatch.setattr(adapter, "__open__", reopen)
    monkeypatch.setattr(f"core.utils.azure_{kind}_conn.time.sleep", lambda _: None)
    args: tuple[Any, ...] = ("dataset", "same.txt", b"data") if operation == "put" else ("dataset", "same.txt")
    with pytest.raises(OSError, match="controlled transport failure"):
        getattr(adapter, operation)(*args)
    assert call.call_count == 3
    assert reopen.call_count == 2


@pytest.mark.parametrize("kind", ["sas", "spn"])
def test_azure_delete_failure_is_not_success(kind: str) -> None:
    adapter = azure_adapter(kind, AzureTransport())
    conn = Mock(spec=type(adapter.conn))
    adapter.conn = conn
    delete = conn.delete_blob if kind == "sas" else conn.delete_file
    delete.side_effect = OSError("controlled delete failure")
    with pytest.raises(OSError, match="controlled delete failure"):
        adapter.rm("dataset", "same.txt", "tenant")


@pytest.mark.parametrize("expires", [0, -1, 604501])
def test_spn_rejects_invalid_url_lifetime(expires: int) -> None:
    transport = AzureTransport()
    adapter = azure_adapter("spn", transport)
    with pytest.raises(ValueError, match="expiry"):
        adapter.get_presigned_url("dataset", "file", expires)
    assert transport.requests == []


def test_sas_cannot_delegate_its_container_token() -> None:
    transport = AzureTransport()
    adapter = azure_adapter("sas", transport)
    with pytest.raises(NotImplementedError, match="object-scoped"):
        adapter.get_presigned_url("dataset", "file", 3600, "tenant")
    assert transport.requests == []
