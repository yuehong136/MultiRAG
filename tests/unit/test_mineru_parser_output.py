"""MinerU 3.x 输出发现与 API 契约钉板。

MinerU v3 起把 content_list.json 写进按解析方法命名的子目录（pipeline→<method>、
hybrid→hybrid_<method>、vlm→vlm），旧的「顶层 / 同名嵌套目录」三连找不到它，
整篇文档会以 FileNotFoundError 收场。

另一条契约是 API 只认 zip：非 zip 响应必须当场报错，而不是记条 warning 后继续，
让失败推迟到后面「找不到输出文件」的迷惑现场。
"""

import json

import pytest
import requests

from deepdoc.parser.mineru_parser import MinerUParser

_STEM = "report"
_CONTENT = [{"type": "text", "text": "hello", "img_path": "images/a.png"}]


@pytest.fixture
def parser():
    return MinerUParser(mineru_api="http://mineru.local")


def _write_content_list(directory, stem=_STEM, payload=None):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stem}_content_list.json"
    target.write_text(json.dumps(payload if payload is not None else _CONTENT), encoding="utf-8")
    return target


def test_read_output_prefers_top_level_file(parser, tmp_path):
    _write_content_list(tmp_path)

    data = parser._read_output(tmp_path, _STEM)

    assert [item["text"] for item in data] == ["hello"]
    assert data[0]["img_path"] == str((tmp_path / "images/a.png").resolve())


@pytest.mark.parametrize(
    ("backend", "method", "subdir"),
    [
        ("pipeline", "auto", "auto"),
        ("pipeline", "ocr", "ocr"),
        ("hybrid", "auto", "hybrid_auto"),
        ("vlm-transformers", "auto", "vlm"),
        ("vlm-vllm-engine", "txt", "vlm"),
    ],
)
def test_read_output_falls_back_to_parse_method_subdir(parser, tmp_path, backend, method, subdir):
    """v3 把输出放进解析方法子目录，且可能再嵌一层文档名目录。"""
    nested = tmp_path / _STEM / subdir
    _write_content_list(nested)

    data = parser._read_output(tmp_path, _STEM, method=method, backend=backend)

    assert [item["text"] for item in data] == ["hello"]
    # 图片路径相对 json 所在目录解析，落在解析方法子目录里
    assert data[0]["img_path"] == str((nested / "images/a.png").resolve())


def test_read_output_falls_back_with_sanitized_stem(parser, tmp_path):
    """文件名被 MinerU 净化过时，子目录回退同样要能命中。"""
    raw_stem = "quarterly report (final)"
    safe_stem = "quarterly_report__final_"
    nested = tmp_path / "auto"
    _write_content_list(nested, stem=safe_stem)

    data = parser._read_output(tmp_path, raw_stem, method="auto", backend="pipeline")

    assert [item["text"] for item in data] == ["hello"]


def test_read_output_reports_every_attempted_path(parser, tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        parser._read_output(tmp_path, _STEM, method="auto", backend="pipeline")

    message = str(excinfo.value)
    assert f"{_STEM}_content_list.json" in message


class _FakeResponse:
    def __init__(self, content_type):
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        return None


def test_run_mineru_api_rejects_non_zip_response(parser, tmp_path, monkeypatch):
    """非 zip 响应当场失败，不再 warning 后继续跑到找不到输出。"""
    from deepdoc.parser import mineru_parser as module

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(module.requests, "post", lambda **kwargs: _FakeResponse("application/json"))

    with pytest.raises(RuntimeError, match="not zip returned from api"):
        parser._run_mineru_api(pdf, tmp_path, module.MinerUParseOptions())


def test_run_mineru_api_wraps_request_errors(parser, tmp_path, monkeypatch):
    from deepdoc.parser import mineru_parser as module

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def _boom(**kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(module.requests, "post", _boom)

    with pytest.raises(RuntimeError, match="api failed with exception"):
        parser._run_mineru_api(pdf, tmp_path, module.MinerUParseOptions())
