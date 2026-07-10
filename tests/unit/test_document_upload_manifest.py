"""``/v1/document/upload`` manifest contract tests."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from api.utils.document_upload import (
    UploadDocumentsManifest,
    UploadManifestValidationError,
    resolve_document_upload_names,
)
from common.constants import RetCode

UPLOAD_URL = "/v1/document/upload"


def _document_module() -> Any:
    """Return the exact dynamically registered document module used by the app."""
    return sys.modules["api.apps.document"]


def _manifest(*documents: dict[str, object]) -> str:
    return json.dumps({"documents": list(documents)}, ensure_ascii=False)


def _multipart_files(*filenames: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (filename, f"content:{filename}".encode(), "application/octet-stream")) for filename in filenames]


@pytest.fixture
def upload_route_stubs(client, monkeypatch):
    """Stub only the route's service boundary and record its effective names."""
    document_routes = _document_module()
    kb = SimpleNamespace(id="dataset-1", tenant_id="dataset-owner", name="dataset")
    calls: dict[str, list[object]] = {"permission": [], "thread_pool": [], "upload": []}

    monkeypatch.setattr(
        document_routes.KnowledgebaseService,
        "get_by_id",
        classmethod(lambda _cls, _db, dataset_id: kb if dataset_id == kb.id else None),
    )

    def check_permission(db, candidate_kb, user_id):
        calls["permission"].append((db, candidate_kb, user_id))
        return True

    monkeypatch.setattr(document_routes, "check_kb_team_permission", check_permission)

    async def run_inline(func, *args, **kwargs):
        calls["thread_pool"].append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(document_routes, "thread_pool_exec", run_inline)

    def upload_document(
        _cls,
        db,
        candidate_kb,
        file_contents,
        user_id,
        labels=None,
    ):
        calls["upload"].append(
            {
                "db": db,
                "kb": candidate_kb,
                "file_contents": file_contents,
                "user_id": user_id,
                "labels": labels,
            }
        )
        uploaded = []
        for index, (blob, name) in enumerate(file_contents):
            uploaded.append(
                (
                    {
                        "id": f"doc-{index}",
                        "kb_id": candidate_kb.id,
                        "name": name,
                        "type": name.rsplit(".", 1)[-1].lower(),
                        "parser_id": "naive",
                        "chunk_num": 0,
                        "token_num": 0,
                    },
                    blob,
                )
            )
        return [], uploaded

    monkeypatch.setattr(document_routes.FileService, "upload_document", classmethod(upload_document))
    return SimpleNamespace(kb=kb, calls=calls, document_routes=document_routes)


def test_manifest_model_normalizes_nfc_and_trims_outer_whitespace():
    manifest = UploadDocumentsManifest.model_validate({"documents": [{"file_index": 0, "name": "  Cafe\u0301.pdf  "}]})

    assert manifest.documents[0].name == "Café.pdf"


@pytest.mark.parametrize(
    "payload",
    [
        {"documents": [{"file_index": 0, "name": "doc.pdf"}], "unknown": True},
        {"documents": [{"file_index": 0, "name": "doc.pdf", "unknown": True}]},
        {"documents": [{"file_index": -1, "name": "doc.pdf"}]},
        {"documents": [{"file_index": 0, "name": "doc.pdf"}, {"file_index": 0, "name": "copy.pdf"}]},
    ],
)
def test_manifest_model_is_strict_and_rejects_duplicate_or_negative_indexes(payload):
    with pytest.raises(ValidationError):
        UploadDocumentsManifest.model_validate(payload)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        ".",
        "..",
        "folder/doc.pdf",
        "folder\\doc.pdf",
        "line\nbreak.pdf",
        "nul\x00byte.pdf",
        "escape\x1b.pdf",
        "文" * 84 + ".pdf",  # 256 UTF-8 bytes
    ],
)
def test_manifest_model_rejects_unsafe_or_overlong_names(name):
    with pytest.raises(ValidationError):
        UploadDocumentsManifest.model_validate({"documents": [{"file_index": 0, "name": name}]})


def test_manifest_model_accepts_name_at_255_utf8_byte_limit():
    name = "a" * 251 + ".pdf"

    manifest = UploadDocumentsManifest.model_validate({"documents": [{"file_index": 0, "name": name}]})

    assert len(manifest.documents[0].name.encode()) == 255


def test_resolve_names_preserves_legacy_filenames_without_manifest():
    assert resolve_document_upload_names(["938472938472.pdf", "notes.TXT"], None) == [
        "938472938472.pdf",
        "notes.TXT",
    ]


def test_resolve_names_maps_out_of_order_manifest_and_accepts_extension_case():
    manifest = UploadDocumentsManifest.model_validate(
        {
            "documents": [
                {"file_index": 1, "name": "会议纪要.txt"},
                {"file_index": 0, "name": "年度报告.PDF"},
            ]
        }
    )

    assert resolve_document_upload_names(["938472.pdf", "837462.TXT"], manifest) == [
        "年度报告.PDF",
        "会议纪要.txt",
    ]


@pytest.mark.parametrize(
    ("source_filenames", "documents"),
    [
        (["one.pdf", "two.pdf"], [{"file_index": 0, "name": "只有一份.pdf"}]),
        (["one.pdf"], [{"file_index": 1, "name": "越界.pdf"}]),
        (["one.pdf"], [{"file_index": 0, "name": "无扩展名"}]),
        (["one.pdf"], [{"file_index": 0, "name": "类型被修改.txt"}]),
        (["no-extension"], [{"file_index": 0, "name": "仍无扩展名"}]),
    ],
)
def test_resolve_names_rejects_incomplete_mapping_and_extension_changes(source_filenames, documents):
    manifest = UploadDocumentsManifest.model_validate({"documents": documents})

    with pytest.raises(UploadManifestValidationError):
        resolve_document_upload_names(source_filenames, manifest)


def test_upload_without_manifest_preserves_legacy_filename_and_response(client, upload_route_stubs):
    response = client.post(
        UPLOAD_URL,
        params={"kb_id": "dataset-1"},
        files=_multipart_files("938472938472.pdf"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == RetCode.SUCCESS
    assert body["data"][0]["name"] == "938472938472.pdf"
    assert body["data"][0]["kb_id"] == "dataset-1"
    assert upload_route_stubs.calls["upload"][0]["file_contents"] == [(b"content:938472938472.pdf", "938472938472.pdf")]
    assert upload_route_stubs.calls["upload"][0]["user_id"] == "user-unit"
    assert upload_route_stubs.calls["upload"][0]["labels"] is None


def test_upload_uses_manifest_names_by_file_index_and_passes_labels(client, upload_route_stubs):
    response = client.post(
        UPLOAD_URL,
        params={
            "kb_id": "dataset-1",
            "labels": json.dumps(["财务", "2025"], ensure_ascii=False),
        },
        data={
            "manifest": _manifest(
                {"file_index": 1, "name": "会议纪要.txt"},
                {"file_index": 0, "name": "年度报告.pdf"},
            ),
        },
        files=_multipart_files("938472.pdf", "837462.txt"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == RetCode.SUCCESS
    assert [document["name"] for document in body["data"]] == ["年度报告.pdf", "会议纪要.txt"]
    upload_call = upload_route_stubs.calls["upload"][0]
    assert upload_call["file_contents"] == [
        (b"content:938472.pdf", "年度报告.pdf"),
        (b"content:837462.txt", "会议纪要.txt"),
    ]
    assert upload_call["labels"] == ["财务", "2025"]
    assert set(upload_call) == {"db", "kb", "file_contents", "user_id", "labels"}


@pytest.mark.parametrize(
    ("filenames", "manifest"),
    [
        (("one.pdf",), "{"),
        (("one.pdf",), json.dumps({"documents": [{"file_index": 0, "name": "one.pdf"}], "unknown": True})),
        (("one.pdf",), json.dumps({"documents": [{"file_index": 0, "name": "one.pdf", "unknown": True}]})),
        (("one.pdf", "two.pdf"), _manifest({"file_index": 0, "name": "one.pdf"})),
        (("one.pdf", "two.pdf"), _manifest({"file_index": 0, "name": "one.pdf"}, {"file_index": 0, "name": "copy.pdf"})),
        (("one.pdf",), _manifest({"file_index": 1, "name": "out-of-range.pdf"})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "   "})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "folder/doc.pdf"})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "line\nbreak.pdf"})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "a" * 252 + ".pdf"})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "missing-extension"})),
        (("one.pdf",), _manifest({"file_index": 0, "name": "changed.txt"})),
    ],
)
def test_invalid_manifest_returns_422_without_upload(client, upload_route_stubs, filenames, manifest):
    response = client.post(
        UPLOAD_URL,
        params={"kb_id": "dataset-1"},
        data={"manifest": manifest},
        files=_multipart_files(*filenames),
    )

    assert response.status_code == 422
    assert upload_route_stubs.calls["upload"] == []
    assert upload_route_stubs.calls["thread_pool"] == []


async def test_mapping_validation_happens_before_file_content_is_read(client, upload_route_stubs, client_user):
    class ReadGuard(BytesIO):
        def __init__(self) -> None:
            super().__init__(b"must-not-be-read")
            self.read_calls = 0

        def read(self, *args, **kwargs):
            self.read_calls += 1
            raise AssertionError("invalid manifest must be rejected before reading files")

    guarded_file = ReadGuard()
    upload = UploadFile(file=guarded_file, filename="source.pdf")
    manifest = UploadDocumentsManifest.model_validate({"documents": [{"file_index": 1, "name": "out-of-range.pdf"}]})

    with pytest.raises(HTTPException) as exc_info:
        await upload_route_stubs.document_routes.upload(
            kb_id="dataset-1",
            files=[upload],
            labels=None,
            manifest=manifest,
            db=object(),
            user=client_user,
        )

    assert exc_info.value.status_code == 422
    assert guarded_file.read_calls == 0


def test_upload_preserves_existing_team_permission_check(client, upload_route_stubs, monkeypatch):
    monkeypatch.setattr(
        upload_route_stubs.document_routes,
        "check_kb_team_permission",
        lambda _db, _kb, _user_id: False,
    )

    response = client.post(
        UPLOAD_URL,
        params={"kb_id": "dataset-1"},
        files=_multipart_files("one.pdf"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert body["retmsg"] == "No authorization."
    assert upload_route_stubs.calls["upload"] == []


def test_openapi_describes_manifest_as_json_form_field(client):
    operation = client.app.openapi()["paths"][UPLOAD_URL]["post"]
    multipart_schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    if "$ref" in multipart_schema:
        component_name = multipart_schema["$ref"].rsplit("/", 1)[-1]
        multipart_schema = client.app.openapi()["components"]["schemas"][component_name]

    files_schema = multipart_schema["properties"]["files"]
    manifest_schema = multipart_schema["properties"]["manifest"]

    assert files_schema["type"] == "array"
    assert files_schema["items"] == {"type": "string", "format": "binary"}
    assert "contentSchema" in json.dumps(manifest_schema)
    assert "UploadDocumentsManifest" in json.dumps(manifest_schema)
    query_parameters = {parameter["name"]: parameter for parameter in operation["parameters"] if parameter["in"] == "query"}
    assert query_parameters["kb_id"]["required"] is True
    assert query_parameters["labels"]["required"] is False
