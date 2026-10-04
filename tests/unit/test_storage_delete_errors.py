"""Production object adapter deletion must not hide backend failures."""

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.utils.minio_conn import MultiRAGMinio
from core.utils.s3_conn import MultiRAGS3


@pytest.mark.parametrize("adapter_type", [MultiRAGMinio, MultiRAGS3])
def test_storage_rm_propagates_failure(adapter_type: Any) -> None:
    adapter = object.__new__(inspect.getclosurevars(adapter_type).nonlocals["cls"])
    client = MagicMock()
    client.remove_object.side_effect = OSError("offline")
    client.delete_object.side_effect = OSError("offline")
    adapter.conn = [client] if adapter_type is MultiRAGS3 else client
    adapter.bucket = "physical-bucket"
    adapter.prefix_path = "prefix"
    with pytest.raises(OSError, match="offline"):
        adapter.rm("logical", "file")
