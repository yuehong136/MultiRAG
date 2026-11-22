"""Keep local-model fallback calls compatible with the installed Hub SDK."""

from importlib import import_module
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from huggingface_hub import snapshot_download

from common import settings


@pytest.mark.parametrize("module_name", ["core.llm.rerank", "core.llm.rerank_model.default_rerank"])
def test_rerank_missing_cache_retries_returned_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str) -> None:
    import FlagEmbedding

    module = import_module(module_name)
    expected = tmp_path / "cached" / "model"
    downloaded = tmp_path / "snapshot"
    model = object()
    loads: list[str] = []
    requests: list[dict[str, Any]] = []

    def load(path: str, **kwargs: Any) -> object:
        loads.append(path)
        if len(loads) == 1:
            raise FileNotFoundError("local model missing")
        return model

    def download(**kwargs: Any) -> str:
        signature(snapshot_download).bind(**kwargs)
        requests.append(kwargs)
        return str(downloaded)

    monkeypatch.setattr(settings, "LIGHTEN", 0)
    monkeypatch.setattr(module.DefaultRerank, "_model", None)
    monkeypatch.setattr(module, "get_home_cache_dir", lambda: str(tmp_path / "cached"))
    monkeypatch.setattr(FlagEmbedding, "FlagReranker", load)
    monkeypatch.setattr(module, "snapshot_download", download)
    instance = module.DefaultRerank(None, "owner/model")
    assert instance._model is model
    assert loads == [str(expected), str(downloaded)]
    assert requests == [{"repo_id": "owner/model", "local_dir": str(expected)}]


@pytest.mark.parametrize("module_name, constructor", [("deepdoc.vision.layout_recognizer", "LayoutRecognizer"), ("deepdoc.vision.table_structure_recognizer", "TableStructureRecognizer")])
def test_deepdoc_recognizer_missing_cache_retries_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str, constructor: str) -> None:
    module = import_module(module_name)
    loads: list[str] = []
    requests: list[dict[str, Any]] = []
    downloaded = str(tmp_path / "snapshot")
    expected = str(tmp_path / "core/res/deepdoc")

    def load(self: Any, labels: list[str], domain: str, directory: str) -> None:
        loads.append(directory)
        if len(loads) == 1:
            raise FileNotFoundError("local model missing")

    def download(**kwargs: Any) -> str:
        signature(snapshot_download).bind(**kwargs)
        requests.append(kwargs)
        return downloaded

    monkeypatch.setattr(module, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(module.Recognizer, "__init__", load)
    monkeypatch.setattr(module, "snapshot_download", download)
    if constructor == "LayoutRecognizer":
        monkeypatch.setattr(module, "get_deepdoc_config", lambda: SimpleNamespace(dla_url=""))
        module.LayoutRecognizer("layout")
    else:
        module.TableStructureRecognizer()
    assert loads == [expected, downloaded]
    assert requests == [{"repo_id": "InfiniFlow/deepdoc", "local_dir": expected}]


def test_ocr_missing_cache_retries_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = import_module("deepdoc.vision.ocr")
    loads: list[tuple[str, str]] = []
    requests: list[dict[str, Any]] = []
    downloaded = str(tmp_path / "snapshot")
    expected = str(tmp_path / "core/res/deepdoc")

    def detector(directory: str, *args: Any) -> object:
        loads.append(("detector", directory))
        if len(loads) == 1:
            raise FileNotFoundError("local model missing")
        return object()

    def recognizer(directory: str, *args: Any) -> object:
        loads.append(("recognizer", directory))
        return object()

    def download(**kwargs: Any) -> str:
        signature(snapshot_download).bind(**kwargs)
        requests.append(kwargs)
        return downloaded

    monkeypatch.setattr(settings, "PARALLEL_DEVICES", 0)
    monkeypatch.setattr(module, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(module, "TextDetector", detector)
    monkeypatch.setattr(module, "TextRecognizer", recognizer)
    monkeypatch.setattr(module, "snapshot_download", download)
    instance = module.OCR()
    assert len(instance.text_detector) == len(instance.text_recognizer) == 1
    assert loads == [("detector", expected), ("detector", downloaded), ("recognizer", downloaded)]
    assert requests == [{"repo_id": "InfiniFlow/deepdoc", "local_dir": expected}]


def test_pdf_concat_missing_cache_retries_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = import_module("deepdoc.parser.pdf_parser")
    loads: list[str] = []
    requests: list[dict[str, Any]] = []
    downloaded = str(tmp_path / "snapshot")
    expected = str(tmp_path / "core/res/deepdoc")

    class Booster:
        def set_param(self, parameters: dict[str, str]) -> None:
            assert parameters == {"device": "cpu"}

        def load_model(self, path: str) -> None:
            loads.append(path)
            if len(loads) == 1:
                raise FileNotFoundError("local model missing")

    def download(**kwargs: Any) -> str:
        signature(snapshot_download).bind(**kwargs)
        requests.append(kwargs)
        return downloaded

    monkeypatch.setattr(settings, "PARALLEL_DEVICES", 0)
    monkeypatch.setenv("LAYOUT_RECOGNIZER_TYPE", "onnx")
    monkeypatch.setattr(module, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(module, "OCR", lambda: object())
    monkeypatch.setattr(module, "LayoutRecognizer", lambda domain: object())
    monkeypatch.setattr(module, "TableStructureRecognizer", lambda: object())
    monkeypatch.setattr(module.xgb, "Booster", Booster)
    monkeypatch.setattr(module, "snapshot_download", download)
    module.RAGFlowPdfParser()
    assert loads == [str(Path(expected) / "updown_concat_xgb.model"), str(Path(downloaded) / "updown_concat_xgb.model")]
    assert requests == [{"repo_id": "InfiniFlow/text_concat_xgb_v1.0", "local_dir": expected}]
