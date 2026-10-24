"""Route module identity is independent of checkout ancestor directory names."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import api.apps as apps


def test_api_named_checkout_keeps_canonical_module_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "api" / "api"
    pages = package / "apps" / "sdk"
    pages.mkdir(parents=True)
    source = pages / "registration_probe.py"
    source.write_text('from fastapi import APIRouter\nrouter = APIRouter()\n@router.get("/probe")\ndef probe():\n    return {"ok": True}\n')
    include_router = Mock()
    monkeypatch.setattr(apps, "__file__", str(package / "apps" / "__init__.py"))
    monkeypatch.setattr(apps, "app", SimpleNamespace(include_router=include_router))
    module_name = "api.apps.sdk.registration_probe"
    monkeypatch.setitem(sys.modules, module_name, None)
    apps.register_page(source)
    module = sys.modules[module_name]
    assert module is not None and module.probe() == {"ok": True}
    include_router.assert_called_once_with(module.router, prefix="/api/v1", tags=["registration_probe"])
