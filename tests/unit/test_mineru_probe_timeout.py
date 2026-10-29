"""The real OCR probe passes its finite timeout into MinerU's HTTP parse."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import requests

from api.db.model_contract import ModelRegistration
from api.db.services.provider_verification import verify_model
from deepdoc.parser import mineru_parser
from deepdoc.parser.mineru_parser import MinerUParseOptions, MinerUParser
from tests.support.resource_journal import ResourceJournal


def registered_temps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    journal = ResourceJournal("mineru-probe-timeout")

    def create(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        path = Path(dir or tmp_path) / ((prefix or "tmp") + uuid4().hex + (suffix or ""))
        journal.event("reserve-parser-directory", path=str(path))
        path.mkdir(mode=0o700)
        paths.append(path)
        return str(path)

    monkeypatch.setattr(mineru_parser.tempfile, "mkdtemp", create)
    return paths


@pytest.mark.parametrize("seconds", [None, 2.5])
def test_mineru_default_and_explicit_http_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seconds: float | None) -> None:
    paths = registered_temps(monkeypatch, tmp_path)
    sample = tmp_path / "fixture.pdf"
    ResourceJournal("mineru-timeout-sample").event("reserve-file", path=str(sample))
    sample.write_bytes(b"%PDF-fixture")
    options = MinerUParseOptions() if seconds is None else MinerUParseOptions(request_timeout=seconds)

    def post(**kwargs: Any) -> Any:
        assert kwargs["timeout"] == (1800 if seconds is None else seconds)
        raise requests.Timeout("controlled timeout")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RuntimeError, match="controlled timeout"):
        MinerUParser(mineru_api="http://fixture.invalid")._run_mineru_api(sample, tmp_path, options)
    # This lower-level method leaves caller-owned output cleanup to parse_pdf.
    for path in paths:
        path.rmdir()
    assert all(not path.exists() for path in paths)


async def test_actual_mineru_probe_http_timeout_and_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = registered_temps(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(requests, "head", lambda *_args, **_kwargs: SimpleNamespace(status_code=200))
    seen: list[float] = []

    def post(**kwargs: Any) -> Any:
        seen.append(kwargs["timeout"])
        assert kwargs["files"]["files"][1].read(4) == b"%PDF"
        raise requests.Timeout("private-diagnostics")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(ValueError, match="Provider connection verification failed") as failure:
        await verify_model(
            "MinerU", '{"api_key":{"mineru_apiserver":"http://fixture.invalid","mineru_delete_output":1}}', "", ModelRegistration(model_name="MinerU", model_types=["ocr"], max_tokens=0)
        )
    assert seen == [2] and "private-diagnostics" not in str(failure.value)
    assert paths and all(not path.exists() for path in paths)
