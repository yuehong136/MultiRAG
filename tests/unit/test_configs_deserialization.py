"""Base64 pickle decoding always applies the existing module restrictions."""

import base64
import os
import pickle
import shlex
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from api.utils.configs import deserialize_b64, serialize_b64
from common.config_utils import CONFIGS


class _WriteMarker:
    def __init__(self, marker: Path, command: bool) -> None:
        self.marker = marker
        self.command = command

    def __reduce__(self) -> tuple[Callable[..., Any], tuple[str]]:
        if self.command:
            return os.system, (f"touch {shlex.quote(str(self.marker))}",)
        return eval, (f"__import__('pathlib').Path({str(self.marker)!r}).write_text('executed')",)


@pytest.mark.parametrize("config", [None, False, True], ids=["default", "disabled", "enabled"])
@pytest.mark.parametrize("text", [False, True], ids=["bytes", "text"])
@pytest.mark.parametrize("command", [False, True], ids=["eval", "system"])
@pytest.mark.parametrize("protocol", [0, pickle.HIGHEST_PROTOCOL], ids=["global", "stack-global"])
def test_unsafe_reduce_is_rejected_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: bool | None, text: bool, command: bool, protocol: int) -> None:
    if config is None:
        monkeypatch.delitem(CONFIGS, "use_deserialize_safe_module", raising=False)
    else:
        monkeypatch.setitem(CONFIGS, "use_deserialize_safe_module", config)
    marker = tmp_path / "must-not-exist"
    payload = base64.b64encode(pickle.dumps(_WriteMarker(marker, command), protocol=protocol))

    try:
        with pytest.raises(pickle.UnpicklingError, match="is forbidden"):
            deserialize_b64(payload.decode("ascii") if text else payload)
    finally:
        assert not marker.exists(), "deserialization executed the reducer before rejecting it"


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize("text", [False, True], ids=["bytes", "text"])
def test_primitive_values_roundtrip(protocol: int, text: bool) -> None:
    original = {"none": None, "bools": [False, True], "zero": 0, "empty": "", "values": (1.5, [1, 2])}
    payload = base64.b64encode(pickle.dumps(original, protocol=protocol))

    assert deserialize_b64(payload.decode("ascii") if text else payload) == original


@pytest.mark.parametrize("protocol", range(3, pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize("original", [np.arange(6, dtype=np.int64).reshape(2, 3), np.array([], dtype=np.float32), np.float64(1.5)], ids=["array", "empty-array", "scalar"])
def test_supported_numpy_roundtrip(protocol: int, original: Any) -> None:
    payload = base64.b64encode(pickle.dumps(original, protocol=protocol))

    restored = deserialize_b64(payload)

    np.testing.assert_array_equal(restored, original)
    assert type(restored) is type(original)
    assert restored.dtype == original.dtype


def test_serializer_preserves_binary_and_builtin_containers() -> None:
    original = {"binary": b"\x00\xff", "set": {1, 2}, "frozen": frozenset({3, 4})}
    assert deserialize_b64(serialize_b64(original)) == original


@pytest.mark.parametrize("buffer_type", [bytearray, memoryview])
def test_encoded_buffers_remain_supported(buffer_type: type[bytearray] | type[memoryview]) -> None:
    assert deserialize_b64(buffer_type(serialize_b64([0, False, ""]))) == [0, False, ""]


def test_general_buffer_protocol_remains_supported() -> None:
    payload = serialize_b64([0, False, ""])
    for encoded in (array("B", payload), np.frombuffer(payload, dtype=np.uint8)):
        assert deserialize_b64(encoded) == [0, False, ""]


def test_unlisted_class_remains_forbidden() -> None:
    with pytest.raises(pickle.UnpicklingError, match="is forbidden"):
        deserialize_b64(serialize_b64(Path("ordinary-path")))


def test_invalid_pickle_remains_an_error() -> None:
    with pytest.raises(pickle.UnpicklingError):
        deserialize_b64(base64.b64encode(b"not a pickle"))
