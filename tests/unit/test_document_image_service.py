"""Image bytes, authorization ordering and strict storage failure contracts."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from beartype.roar import BeartypeCallHintParamViolation
from minio import S3Error
from PIL import Image

from api.db.db_models import Knowledgebase
from api.db.services import document_image_service as service
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, Principal, TenantMembershipEvidence, build_principal_from_authenticated_actor
from common import resources
from core.utils.encrypted_storage import EncryptedStorageWrapper
from core.utils.minio_conn import MultiRAGMinio


def principal(owner: str = "owner", source: AuthenticationSource = AuthenticationSource.WEB_SESSION) -> Principal:
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=owner),
        membership=TenantMembershipEvidence(platform_user_id=owner, tenant_id=owner),
        authentication=AuthenticationContext(source=source, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )


def image_bytes(fmt: str = "PNG") -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format=fmt)
    return stream.getvalue()


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp"), ("BMP", "image/bmp")])
def test_verified_raster_types_are_actual_bytes(fmt: str, mime: str) -> None:
    data = image_bytes(fmt)
    result = service._raster(data)
    assert result.data == data and result.media_type == mime
    with pytest.raises(FrozenInstanceError):
        result.data = b""  # type: ignore[misc]


@pytest.mark.parametrize("data", [b"", b"unknown", b"<html>image.png</html>", b"<svg></svg>", b"\x89PNG\r\n\x1a\ntruncated", b"\xff\xd8\xff", b"GIF89a", b"RIFFabcdWEBP", b"BM"])
def test_no_empty_html_svg_or_magic_only_success(data: bytes) -> None:
    with pytest.raises(service.InvalidImageBytes, match=r"^Image data is invalid\.$"):
        service._raster(data)


@pytest.mark.parametrize("value", ["", " ", "kb", "-key", "kb-", "kb- ", "kb-key\n", "x" * 1025])
def test_invalid_image_id(value: str) -> None:
    with pytest.raises(service.InvalidImageInput):
        service._split_image_id(value)


def test_first_hyphen_preserves_key() -> None:
    assert service._split_image_id("kb-page-crop-name.png") == ("kb", "page-crop-name.png")


@pytest.mark.parametrize("ids", [[], [""], [" "], ["doc"] * 101])
async def test_thumbnail_input_limits(async_db: Any, ids: Any) -> None:
    with pytest.raises(service.InvalidImageInput):
        await service.list_thumbnails(async_db, principal(), ids)


async def test_thumbnail_filter_order_inline_empty_and_urls(async_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        SimpleNamespace(id=k, kb_id=kb, thumbnail=thumb)
        for k, kb, thumb in [("url", "kb", "a-b.png"), ("inline", "kb", "data:image/png;base64,xx"), ("none", "kb", None), ("empty", "kb", ""), ("foreign", "foreignkb", "private.png")]
    ]
    monkeypatch.setattr(async_db, "execute", AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    calls: list[str] = []

    async def permitted(db: Any, actor: Principal, kb: str) -> Any:
        calls.append(kb)
        return SimpleNamespace(id=kb) if kb == "kb" else None

    monkeypatch.setattr(service, "_readable_dataset", permitted)
    result = await service.list_thumbnails(async_db, principal(), ["empty", "url", "url", "missing", "inline", "foreign", "none"])
    assert result == {"empty": "", "url": "/api/v1/documents/images/kb-a-b.png", "inline": "data:image/png;base64,xx", "none": None}
    assert list(result) == ["empty", "url", "inline", "none"] and calls == ["kb", "foreignkb"]


@pytest.mark.parametrize("role,permitted", [("owner", True), ("normal", True), ("admin", True), ("invite", False), (None, False), ("unknown", False)])
async def test_fresh_membership_role_check(async_db: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, permitted: bool) -> None:
    kb = Knowledgebase(id="kb")
    monkeypatch.setattr(async_db, "execute", AsyncMock(return_value=SimpleNamespace(first=lambda: (kb, role))))
    assert (await service._readable_dataset(async_db, principal(), "kb") is kb) is permitted


@pytest.mark.parametrize("expected,authorized", [("another", True), (None, False)])
async def test_denied_before_index_or_object_read(async_db: Any, monkeypatch: pytest.MonkeyPatch, expected: str | None, authorized: bool) -> None:
    monkeypatch.setattr(service, "_readable_dataset", AsyncMock(return_value=SimpleNamespace(id="kb") if authorized else None))

    def forbidden(*args: Any) -> Any:
        pytest.fail("unauthorized backend read")

    monkeypatch.setattr(service, "_registered_chunk_documents", forbidden)
    monkeypatch.setattr(service, "_read_object", forbidden)
    with pytest.raises(service.ImageUnavailable):
        await service.read_dataset_image(async_db, principal(), "kb-private", expected)


async def test_thumbnail_exact_registration_without_index(async_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_readable_dataset", AsyncMock(return_value=SimpleNamespace(id="kb")))
    monkeypatch.setattr(async_db, "scalar", AsyncMock(return_value="doc-status0"))
    data = image_bytes("GIF")
    calls: list[tuple[str, str]] = []

    def read(namespace: str, key: str) -> bytes:
        calls.append((namespace, key))
        return data

    monkeypatch.setattr(service, "_read_object", read)
    result = await service.read_dataset_image(async_db, principal(), "kb-multi-hyphen.wrongjpg")
    assert result.data == data and result.media_type == "image/gif" and calls == [("kb", "multi-hyphen.wrongjpg")]


@pytest.mark.parametrize("candidate", [None, "doc"])
async def test_index_registration_needs_sql_document_same_dataset(async_db: Any, monkeypatch: pytest.MonkeyPatch, candidate: str | None) -> None:
    monkeypatch.setattr(service, "_readable_dataset", AsyncMock(return_value=SimpleNamespace(id="kb", tenant_id="tenant", name="realname")))
    monkeypatch.setattr(async_db, "scalar", AsyncMock(side_effect=[None, candidate]))
    monkeypatch.setattr(service, "_registered_chunk_documents", lambda *args: ["doc"])
    calls: list[Any] = []
    monkeypatch.setattr(service, "_read_object", lambda *args: calls.append(args) or image_bytes())
    if candidate is None:
        with pytest.raises(service.ImageUnavailable):
            await service.read_dataset_image(async_db, principal(), "kb-key")
        assert not calls
    else:
        assert (await service.read_dataset_image(async_db, principal(), "kb-key")).media_type == "image/png"


def test_index_exact_img_id_real_name_no_available_or_mother_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []

    def search(*args: Any) -> Any:
        captured.append(args)
        return None

    store = SimpleNamespace(
        db_type=lambda: "infinity",
        search=search,
        get_fields=lambda *args: {"distance": [], "a": {"img_id": "kb-key", "doc_id": "doc", "kb_id": "kb", "available_int": 0}, "b": {"img_id": "foreign", "doc_id": "foreign"}},
    )
    monkeypatch.setitem(resources._state, "doc_store", store)
    assert service._registered_chunk_documents("tenant", "realname", "kb", "kb-key") == ["doc"]
    assert captured[0][2] == {"img_id": "kb-key", "kb_id": "kb"} and captured[0][7:9] == (["multirag_tenant_realname"], ["kb"])


@pytest.mark.parametrize("kind,exc", [("missing", service.ImageUnavailable), ("transport", service.ImageStorageFailure), ("type", service.InvalidImageBytes)])
def test_storage_adapter_failure_is_safe(monkeypatch: pytest.MonkeyPatch, kind: str, exc: type[Exception]) -> None:
    def read(*args: Any) -> Any:
        if kind == "transport":
            raise RuntimeError("SECRET/private/bucket/key")
        return None if kind == "missing" else "not bytes"

    monkeypatch.setitem(resources._state, "storage", SimpleNamespace(get_bytes=read))
    with pytest.raises(exc) as error:
        service._read_object("private", "secret-key")
    assert "SECRET" not in str(error.value) and "private" not in str(error.value)


@pytest.mark.parametrize("file_id", ["", "other-file", "A" * 32, "../" + "a" * 32])
async def test_runtime_invalid_ids(file_id: str) -> None:
    with pytest.raises(service.InvalidImageInput):
        await service.read_runtime_image(principal(), file_id)


@pytest.mark.parametrize("kind", ["good", "size", "changed-sidecar", "missing-sidecar", "tampered-owner"])
async def test_runtime_namespace_real_resolver_and_descriptor(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    file_id, data = "a" * 32, image_bytes()
    descriptor = {"id": file_id, "created_by": "owner", "size": len(data), "name": "misleading.jpg", "mime_type": "image/jpeg", "extension": "jpg", "created_at": 1, "preview_url": None}
    if kind == "size":
        descriptor["size"] += 1
    if kind == "tampered-owner":
        descriptor["created_by"] = "other"
    calls: list[tuple[str, str]] = []

    def read(bucket: str, key: str) -> bytes | None:
        calls.append((bucket, key))
        if key.endswith("upload.json"):
            return None if kind == "missing-sidecar" else json.dumps(descriptor).encode()
        return data

    def legacy(bucket: str, key: str) -> bytes | None:
        if kind == "changed-sidecar":
            return json.dumps({**descriptor, "name": "changed"}).encode()
        return read(bucket, key)

    monkeypatch.setitem(resources._state, "storage", SimpleNamespace(get_bytes=read, get=legacy, obj_exist=lambda *args: True))
    if kind == "good":
        result = await service.read_runtime_image(principal(), file_id)
        assert result.data == data and result.media_type == "image/png"
    else:
        with pytest.raises(service.InvalidImageBytes if kind == "size" else service.ImageUnavailable):
            await service.read_runtime_image(principal(), file_id)
    assert all(bucket == "owner-downloads" for bucket, _ in calls)


def minio_adapter() -> Any:
    # The singleton factory closes over the real class; bypass connection setup.
    cls = next(cell.cell_contents for cell in MultiRAGMinio.__closure__ or () if isinstance(cell.cell_contents, type))
    return object.__new__(cls)


@pytest.mark.parametrize(
    "default,prefix,expected",
    [(None, None, ("logical", "a-b")), ("physical", None, ("physical", "logical/a-b")), ("physical", "prefix", ("physical", "prefix/logical/a-b")), (None, "prefix", ("logical", "prefix/a-b"))],
)
def test_strict_minio_mapping_closes_response(default: str | None, prefix: str | None, expected: tuple[str, str]) -> None:
    adapter = minio_adapter()
    adapter.bucket, adapter.prefix_path = default, prefix
    calls: list[Any] = []
    response = SimpleNamespace(read=lambda: b"data", close=lambda: calls.append("close"), release_conn=lambda: calls.append("release"))
    adapter.conn = SimpleNamespace(get_object=lambda *args: calls.append(args) or response)
    assert adapter.get_bytes("logical", "a-b") == b"data"
    assert calls == [expected, "close", "release"]


@pytest.mark.parametrize("failure,missing", [("NoSuchKey", True), ("NoSuchBucket", True), ("AccessDenied", False), ("transport", False)])
def test_strict_minio_missing_vs_transport(failure: str, missing: bool) -> None:
    adapter = minio_adapter()
    adapter.bucket = adapter.prefix_path = None

    def get(*args: Any) -> Any:
        if failure == "transport":
            raise OSError("secret host")
        raise S3Error(failure, "private key", "resource", "request", "host", None)

    adapter.conn = SimpleNamespace(get_object=get)
    if missing:
        assert adapter.get_bytes("logical", "key") is None
    else:
        with pytest.raises(RuntimeError, match=r"^Object storage read failed\.$"):
            adapter.get_bytes("logical", "key")


@pytest.mark.parametrize("mode", ["good", "missing", "transport", "decrypt", "disabled"])
@pytest.mark.parametrize("strict", [True, False])
def test_strict_encrypted_reads_keep_cipher_and_fail_safely(mode: str, strict: bool) -> None:
    wrapper = object.__new__(EncryptedStorageWrapper)
    wrapper.encryption_enabled = mode != "disabled"

    def read(*args: Any) -> bytes | None:
        if mode == "transport":
            raise OSError("secret")
        return None if mode == "missing" else b"cipher"

    def decrypt(data: bytes) -> bytes:
        assert data == b"cipher"
        if mode == "decrypt":
            raise ValueError("secret crypto detail")
        return b"plain"

    wrapper.storage_impl = SimpleNamespace(**{"get_bytes" if strict else "get": read})
    wrapper.crypto = SimpleNamespace(decrypt=decrypt)
    if mode in {"transport", "decrypt"}:
        with pytest.raises(RuntimeError, match=r"^Object storage read failed\.$"):
            wrapper.get_bytes("logical", "key")
    else:
        assert wrapper.get_bytes("logical", "key") == (None if mode == "missing" else b"cipher" if mode == "disabled" else b"plain")


@pytest.mark.parametrize("ids", [[1], "doc", None])
async def test_wrong_typed_inputs_rejected_by_runtime_annotation(async_db: Any, ids: Any) -> None:
    with pytest.raises(BeartypeCallHintParamViolation):
        await service.list_thumbnails(async_db, principal(), ids)


@pytest.mark.parametrize("fmt", ["JPEG", "GIF", "BMP", "WEBP"])
def test_valid_header_with_truncated_pixels_is_rejected(fmt: str) -> None:
    with pytest.raises(service.InvalidImageBytes):
        service._raster(image_bytes(fmt)[:-8])


def test_real_milvus_search_payload_without_broken_get_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any) -> Any:
        pytest.fail("Milvus get_fields has incompatible runtime annotation")

    rows = [{"pk": "chunk", "img_id": "kb-key", "doc_id": "doc", "kb_id": "kb"}, {"pk": "foreign", "img_id": "kb-key", "doc_id": "doc", "kb_id": "foreign"}]
    monkeypatch.setitem(resources._state, "doc_store", SimpleNamespace(db_type=lambda: "milvus", search=lambda *args: (rows, 2), get_fields=forbidden))
    assert service._registered_chunk_documents("tenant", "name", "kb", "kb-key") == ["doc"]
