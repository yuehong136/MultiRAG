"""Cached test assets are trusted only after digest verification."""

import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from scripts import provision_test_assets as assets


def test_corrupt_cache_and_partial_download_never_replace_verified_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "sample.onnx"
    target.write_bytes(b"old")
    monkeypatch.setattr(assets, "ASSETS", {target.name: hashlib.sha256(b"correct").hexdigest()})
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(b"wrong"))
    with pytest.raises(RuntimeError, match="verified OCR asset"):
        assets.provision(tmp_path)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(b"correct"))
    assets.provision(tmp_path)
    assert target.read_bytes() == b"correct"


def test_verified_cache_does_not_need_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "sample.onnx"
    target.write_bytes(b"correct")
    monkeypatch.setattr(assets, "ASSETS", {target.name: hashlib.sha256(b"correct").hexdigest()})

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("verified cache should not make a request")

    monkeypatch.setattr(assets.urllib.request, "urlopen", no_network)
    assets.provision(tmp_path)
