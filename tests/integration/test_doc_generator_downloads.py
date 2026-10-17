"""DocGenerator -> Message -> owned MinIO objects -> protected HTTP download.

Only the format converter is controlled; file IO, component output, object
storage and download authorization run through production implementations.
"""

import base64
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import uvicorn  # noqa: F401 -- initialize before legacy component nest_asyncio patches
from sqlalchemy.orm import sessionmaker

from agent.component.docs_generator import DocGenerator, DocGeneratorParam
from agent.component.message import Message
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("include", [False, True])
def test_generated_download_bytes_and_owner_scope(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, include: bool) -> None:
    from api import apps

    env = runtime_upload_api
    # The reused fixture binds async request DBs. The legacy JWT user loader
    # additionally opens SessionLocal; keep that real lookup in the scratch DB.
    monkeypatch.setattr(apps, "SessionLocal", sessionmaker(bind=env["engine"]))
    generator = object.__new__(DocGenerator)
    generator._param = DocGeneratorParam()
    generator._param.include_download_info_in_content = include
    generator._param.content = "Confidential generation marker 中文"
    generator._param.filename = "generated-report"
    generator._canvas = SimpleNamespace(get_tenant_id=lambda: env["owners"][0])
    generator._default_output_directory = str(tmp_path / "generated")
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    foreign_headers = {"Authorization": "Bearer " + apps.manager.create_access_token(data={"sub": f"{env['owners'][1]}@upload.test"})}
    snapshots: list[dict[str, Any]] = []

    for fmt, extension, mime in (
        ("pdf", "pdf", "application/pdf"),
        ("docx", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("txt", "txt", "text/plain"),
        ("markdown", "md", "text/markdown"),
        ("html", "html", "text/html"),
    ):
        generator._param.output_format = fmt
        expected = (fmt + "\x00\n" + generator._param.content).encode("utf-8")
        generated: dict[str, Any] = {}

        def convert(content: str) -> tuple[str, bytes]:
            assert content == generator._param.content
            path, binary = generator._write_bytes_output(expected, extension)
            generated.update(path=path, bytes=Path(path).read_bytes())
            return path, binary

        monkeypatch.setattr(generator, f"_generate_{fmt}", convert)
        with caplog.at_level(logging.INFO):
            info = generator._invoke()
        payload = json.loads(generator.output("download"))
        assert payload == info
        assert set(info) == {"doc_id", "filename", "mime_type", "size", "base64", "include_download_info_in_content"}
        assert info["filename"] == f"generated-report.{extension}" and info["mime_type"] == mime
        physical = read_object(env["storage"], env["bucket"], f"{env['owners'][0]}/{info['doc_id']}")
        assert base64.b64decode(info["base64"], validate=True) == generated["bytes"] == physical == expected
        assert info["size"] == len(physical)
        assert not Path(generated["path"]).exists()
        downloads: list[dict[str, Any]] = []
        message = object.__new__(Message)
        text = message._stringify_message_value(generator.output("download"), downloads=downloads)
        assert json.loads(text) == Message._normalize_download_info(info) if include else text == ""
        assert downloads == [Message._normalize_download_info(info)]

        url = f"{env['base']}/v1/document/download/{info['doc_id']}"
        response = requests.get(url, headers=headers, params={"ext": extension}, timeout=30)
        assert response.status_code == 200 and response.content == physical
        if extension == "html":
            assert "attachment" in response.headers["Content-Disposition"]
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert requests.get(url, params={"ext": extension}, timeout=30).status_code == 401
        # Identical object IDs in another namespace must not expose the owner bytes.
        env["storage_adapter"].put(env["owners"][1], info["doc_id"], b"other tenant object")
        foreign = requests.get(url, headers=foreign_headers, params={"ext": extension}, timeout=30)
        assert foreign.status_code == 200 and foreign.content == b"other tenant object"
        assert generator._param.content not in caplog.text and info["base64"] not in caplog.text
        snapshots.append({"format": fmt, "doc_id": info["doc_id"], "size": info["size"], "sha256": hashlib.sha256(physical).hexdigest(), "file_storage_base64_http_equal": True})

    env["record"]["doc_generator_readback"] = snapshots
    env["record_path"].write_text(json.dumps(env["record"], indent=2))
