"""Exercise output discovery against real files, without a remote MinerU server."""

import json
import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepdoc.parser.mineru_parser import MinerUBackend, MinerUParseOptions, MinerUParser


def write_output(root: Path, relative: str, text: str = "selected") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "text", "text": text}
    for key in ("img_path", "table_img_path", "equation_img_path"):
        asset = target.parent / "images" / f"{key}.png"
        asset.parent.mkdir(exist_ok=True)
        asset.write_bytes(f"{text}:{key}".encode())
        payload[key] = f"images/{key}.png"
    target.write_text(json.dumps([payload]), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("stem", "relative", "backend", "method"),
    [
        ("report", "report_content_list.json", "pipeline", "auto"),
        ("report final", "report final_content_list.json", "pipeline", "auto"),
        ("report final", "report_final_content_list.json", "pipeline", "auto"),
        ("report final", "report_final/report_final_content_list.json", "pipeline", "auto"),
        ("report[1]", "wrapper/auto/report[1]_content_list.json", "pipeline", "auto"),
        ("report", "wrapper/report/auto/content_list.json", "pipeline", "auto"),
        ("report final", "wrapper/report final/ocr/content_list.json", "pipeline", "ocr"),
        ("report final", "wrapper/report_final/txt/content_list.json", "pipeline", "txt"),
        ("report", "wrapper/report/hybrid_auto/content_list.json", "hybrid-auto-engine", "auto"),
        ("report", "wrapper/report/vlm/content_list.json", "vlm-http-client", "ocr"),
        ("report", "wrapper/report/vlm/renamed_content_list.json", "vlm-http-client", "auto"),
        ("report", "wrapper/report/content_list.json", "unknown", "auto"),
        ("report", "wrapper/report/renamed_content_list.json", "unknown", "auto"),
        ("report", "wrapper/auto/report_001_content_list.json", "pipeline", "auto"),
        ("report final", "wrapper/report_final-001_content_list.json", "unknown", "auto"),
        ("report", "content_list.json", "unknown", "auto"),
        ("report", "auto/content_list.json", "pipeline", "auto"),
        ("report", "hybrid_ocr/content_list.json", "hybrid", "ocr"),
        ("report", "vlm/content_list.json", "vlm-http-client", "txt"),
    ],
)
def test_reads_disk_layout_and_assets(tmp_path: Path, stem: str, relative: str, backend: str, method: str) -> None:
    selected = write_output(tmp_path, relative)
    data = MinerUParser()._read_output(tmp_path, stem, method=method, backend=backend)
    assert data[0]["text"] == "selected"
    for key in ("img_path", "table_img_path", "equation_img_path"):
        asset = Path(data[0][key])
        assert asset == (selected.parent / "images" / f"{key}.png").resolve()
        assert asset.read_bytes() == f"selected:{key}".encode()


@pytest.mark.parametrize(
    "relative",
    [
        "other/auto/content_list.json",
        "other/content_list.json",
        "wrapper/content_list.json",
        "other/auto/other_content_list.json",
        "report2_content_list.json",
        "auto/reporting_content_list.json",
        "report/other/content_list.json",
    ],
)
def test_rejects_unrelated_outputs(tmp_path: Path, relative: str) -> None:
    write_output(tmp_path, relative, "wrong document")
    with pytest.raises(FileNotFoundError, match="Missing output file"):
        MinerUParser()._read_output(tmp_path, "report")


@pytest.mark.parametrize("relative", ["content_list.json", "auto/content_list.json"])
def test_unscoped_generic_requires_single_document_output(tmp_path: Path, relative: str) -> None:
    write_output(tmp_path, relative)
    write_output(tmp_path, "other/auto/other_content_list.json", "wrong document")
    with pytest.raises(FileNotFoundError, match="Missing output file"):
        MinerUParser()._read_output(tmp_path, "report")


@pytest.mark.parametrize(
    ("winner", "loser", "stem"),
    [
        ("report final_content_list.json", "report_final_content_list.json", "report final"),
        ("report_content_list.json", "report/auto/content_list.json", "report"),
        ("report/auto/report_content_list.json", "report/auto/content_list.json", "report"),
        ("report/auto/content_list.json", "auto/content_list.json", "report"),
        ("report/auto/content_list.json", "aaa/auto/content_list.json", "report"),
        ("report/auto/content_list.json", "report/auto/renamed_content_list.json", "report"),
        ("report/auto/report_001_content_list.json", "aaa/auto/other_content_list.json", "report"),
    ],
)
def test_prefers_identified_document(tmp_path: Path, winner: str, loser: str, stem: str) -> None:
    write_output(tmp_path, loser, "wrong document")
    write_output(tmp_path, winner)
    assert MinerUParser()._read_output(tmp_path, stem)[0]["text"] == "selected"


@pytest.mark.parametrize(
    "relatives",
    [
        ("a/auto/report_content_list.json", "b/auto/report_content_list.json"),
        ("a/report/auto/content_list.json", "b/report/auto/content_list.json"),
        ("report/auto/first_content_list.json", "report/auto/second_content_list.json"),
        ("auto/report_001_content_list.json", "auto/report_002_content_list.json"),
    ],
)
def test_ambiguous_outputs_fail_without_reading_json(tmp_path: Path, relatives: tuple[str, str]) -> None:
    for relative in reversed(relatives):
        path = write_output(tmp_path, relative)
        path.write_text("invalid JSON must not be opened", encoding="utf-8")
    with pytest.raises(ValueError, match="Ambiguous output files") as error:
        MinerUParser()._read_output(tmp_path, "report")
    for relative in relatives:
        assert str(tmp_path / relative) in str(error.value)


@pytest.mark.parametrize("backend", ["pipeline", "hybrid", "vlm-http-client", "unknown"])
def test_missing_output_has_no_uninitialized_variables(tmp_path: Path, backend: str) -> None:
    with pytest.raises(FileNotFoundError, match="Missing output file"):
        MinerUParser()._read_output(tmp_path, "report", backend=backend)


def test_directory_named_like_json_is_not_selected(tmp_path: Path) -> None:
    (tmp_path / "report_content_list.json").mkdir()
    write_output(tmp_path, "report/auto/content_list.json")
    assert MinerUParser()._read_output(tmp_path, "report")[0]["text"] == "selected"


def test_logs_actual_selected_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    selected = write_output(tmp_path, "wrapper/report/vlm/content_list.json")
    with caplog.at_level(logging.INFO, logger="MinerUParser"):
        MinerUParser()._read_output(tmp_path, "report", backend="vlm-http-client")
    assert f"Reading output file: {selected}" in caplog.text


@pytest.mark.parametrize(
    ("stem", "relative", "backend", "extracted"),
    [
        ("report", "report/auto/content_list.json", "pipeline", "auto/content_list.json"),
        ("report(final)", "report_final_/auto/content_list.json", "pipeline", "report_final_/auto/content_list.json"),
        ("report", "wrapper/report/vlm/renamed_content_list.json", "vlm-http-client", "wrapper/report/vlm/renamed_content_list.json"),
    ],
)
def test_api_zip_to_real_output_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stem: str, relative: str, backend: str, extracted: str) -> None:
    source = tmp_path / "zip-source"
    write_output(source, relative)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(source))
    archive.seek(0)
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {"Content-Type": "application/zip"}
    response.raw = archive

    def post(**kwargs: Any) -> MagicMock:
        assert kwargs["files"]["files"][0] == f"{stem}.pdf"
        assert kwargs["data"]["backend"] == backend
        return response

    monkeypatch.setattr("deepdoc.parser.mineru_parser.requests.post", post)
    parser = MinerUParser(mineru_api="http://mineru.invalid")
    pdf = tmp_path / f"{stem}.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    destination = tmp_path / "extracted"
    destination.mkdir()
    output_dir = parser._run_mineru_api(pdf, destination, MinerUParseOptions(backend=MinerUBackend(backend)))
    assert (output_dir / extracted).is_file()
    data = parser._read_output(output_dir, stem, backend=backend)
    assert data[0]["text"] == "selected"
    for key in ("img_path", "table_img_path", "equation_img_path"):
        path = Path(data[0][key])
        assert path == (output_dir / extracted).parent / "images" / f"{key}.png"
        assert path.read_bytes() == f"selected:{key}".encode()
