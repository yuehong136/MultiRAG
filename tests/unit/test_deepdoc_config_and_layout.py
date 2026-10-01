from typing import Any

import pytest
from PIL import Image

from common import app_config, deepdoc_config
from common.app_config import AppConfig, AppConfigError
from deepdoc.vision import LayoutRecognizer as ExportedLayoutRecognizer
from deepdoc.vision import layout_recognizer


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    cfg = AppConfig()
    cfg._raw = {}
    monkeypatch.setattr(deepdoc_config, "get_app_config", lambda: cfg)
    for variable in ("DEEPDOC_URL", "TENSORRT_DLA_SVR", "PDF_PARSER_PAGE_BATCH_SIZE"):
        monkeypatch.delenv(variable, raising=False)
    return cfg


def test_deepdoc_defaults_and_canonical_config_priority(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert deepdoc_config.get_deepdoc_config().page_batch_size == 50
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", "7")
    monkeypatch.setenv("TENSORRT_DLA_SVR", "legacy")
    assert deepdoc_config.get_deepdoc_config().page_batch_size == 7
    monkeypatch.setenv("DEEPDOC_URL", "current")
    assert deepdoc_config.get_deepdoc_config().dla_url == "current"
    config._raw = {"deepdoc": {"page_batch_size": 9, "dla_url": "configured"}}
    assert deepdoc_config.get_deepdoc_config().page_batch_size == 9
    assert deepdoc_config.get_deepdoc_config().dla_url == "configured"
    config._raw["deepdoc"]["dla_url"] = ""
    assert deepdoc_config.get_deepdoc_config().dla_url == ""  # explicit disable


def test_canonical_environment_overrides_file_section(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIRAG_DEEPDOC__PAGE_BATCH_SIZE", "3")
    monkeypatch.setenv("MULTIRAG_DEEPDOC__DLA_URL", "canonical")
    merged: dict[str, Any] = {"deepdoc": {"page_batch_size": 11, "dla_url": "file"}}
    app_config._apply_env_overlay(merged)
    config._raw = merged
    assert deepdoc_config.get_deepdoc_config().page_batch_size == 3
    assert deepdoc_config.get_deepdoc_config().dla_url == "canonical"
    monkeypatch.setenv("MULTIRAG_DEEPDOC__DLA_URL", "")
    app_config._apply_env_overlay(merged)
    assert deepdoc_config.get_deepdoc_config().dla_url == ""


@pytest.mark.parametrize("value", ["invalid", "0", "-1"])
def test_batch_size_validation(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", value)
    with pytest.raises(AppConfigError, match="page_batch_size"):
        deepdoc_config.get_deepdoc_config()


class RemoteClient:
    def predict(self, images: list[Image.Image]) -> list[list[dict[str, Any]]]:
        return [[{"type": "text", "score": 0.99, "bbox": [1, 1, 90, 40]}] for _ in images]


@pytest.mark.parametrize("variable", ["DEEPDOC_URL", "TENSORRT_DLA_SVR"])
@pytest.mark.parametrize("recognizer_type", [layout_recognizer.LayoutRecognizer, ExportedLayoutRecognizer])
def test_remote_branch_skips_models_download_and_runs_existing_calls(monkeypatch: pytest.MonkeyPatch, variable: str, recognizer_type: Any) -> None:
    monkeypatch.setenv(variable, "remote-config")
    urls: list[str] = []

    def client(url: str) -> RemoteClient:
        urls.append(url)
        return RemoteClient()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Remote selection must not load or download a local model")

    monkeypatch.setattr(layout_recognizer.LayoutRecognizer, "_create_remote_client", staticmethod(client))
    monkeypatch.setattr(layout_recognizer.Recognizer, "__init__", forbidden)
    monkeypatch.setattr(layout_recognizer, "snapshot_download", forbidden)
    recognizer = recognizer_type("layout")
    image = Image.new("RGB", (100, 100))
    raw = recognizer.forward([image])
    assert raw[0][0]["type"] == "text"
    boxes, layouts = recognizer([image], [[{"text": "Body", "x0": 2, "x1": 30, "top": 2, "bottom": 15, "page_number": 1}]], scale_factor=1)
    assert boxes[0]["layout_type"] == "text" and layouts[0][0]["type"] == "text"
    assert urls == ["remote-config"]


def test_missing_remote_client_fails_before_models_or_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPDOC_URL", "unavailable")

    def missing(module: str) -> Any:
        raise ModuleNotFoundError(name=module)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Missing remote client must fail before local initialization")

    monkeypatch.setattr(layout_recognizer, "import_module", missing)
    monkeypatch.setattr(layout_recognizer.Recognizer, "__init__", forbidden)
    monkeypatch.setattr(layout_recognizer, "snapshot_download", forbidden)
    with pytest.raises(RuntimeError, match="deployment-provided"):
        ExportedLayoutRecognizer("layout")


@pytest.mark.parametrize("download_needed", [False, True])
def test_local_mode_preserves_model_fallback(monkeypatch: pytest.MonkeyPatch, download_needed: bool) -> None:
    initialized: list[str] = []
    downloads: list[str] = []

    def model_init(self: Any, labels: list[str], domain: str, directory: str) -> None:
        initialized.append(directory)
        if download_needed and len(initialized) == 1:
            raise ValueError("model missing")

    def download(**kwargs: Any) -> str:
        downloads.append(kwargs["repo_id"])
        return "/test/downloaded"

    monkeypatch.setattr(layout_recognizer.Recognizer, "__init__", model_init)
    monkeypatch.setattr(layout_recognizer, "snapshot_download", download)
    recognizer = ExportedLayoutRecognizer("layout")
    assert recognizer.client is None
    assert len(initialized) == (2 if download_needed else 1)
    assert downloads == (["InfiniFlow/deepdoc"] if download_needed else [])
