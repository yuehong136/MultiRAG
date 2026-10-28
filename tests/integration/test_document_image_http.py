"""Real authenticated HTTP reads and complete owned SQL/index/object/queue snapshots."""

import os
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import urllib3
from minio import Minio, S3Error

from api.db.db_models import Document
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from common import resources, settings
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_image_http import _retired_binary_matrix, _save, _setup
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import _image, _snapshot
from tests.support.document_image_read_service import image_resources as image_resources
from tests.support.smoke import smoke_command


def test_authenticated_image_http_full_storage_no_writes(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = image_http_api, image_http_api["ids"]
    setup = _setup(env)
    _retired_binary_matrix(env)
    record: dict[str, Any] = {
        "chunks": setup["chunks"],
        "runtimes": setup["runtimes"],
        "calls": [],
        "auth": "actual JWT and API token; no identity override",
        "fault_boundary": "denied/read/close/index controlled; closed MinIO loopback and ciphertext real",
    }
    missing_key = env["adapter"]._resolve_bucket_and_path(ids["kb"], "missing-object")[1]
    with pytest.raises(S3Error) as missing:
        env["client"].stat_object(env["bucket"], missing_key)
    assert missing.value.code == "NoSuchKey"
    record["physical_missing"] = {"bucket": env["bucket"], "key": missing_key, "storage_code": missing.value.code, "http_404_semantics": "availability mask, not physical proof"}
    path = env["evidence"] / f"{ids['kb']}.http-readback.json"
    before = record["before"] = _snapshot(env)
    _save(path, record)
    guards: list[str] = []
    sql_reads: list[str] = []

    def forbid(*args: Any, **kwargs: Any) -> Any:
        guards.append("write")
        raise AssertionError("image HTTP read attempted a write")

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        sql_reads.append(statement.lstrip().split()[0].upper())
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            guards.append("SQL write")
            raise AssertionError("image HTTP attempted SQL DML")

    def get(
        url: str, role: str | None = "owner", params: Any = None, status: int = 200, code: int | None = None, binary: bytes | None = None, mime: str | None = None, feature: bool = True
    ) -> requests.Response:
        headers = {"Authorization": "Bearer " + env["tokens"][role]} if role else {}
        response = requests.get(env["base"] + url, headers=headers, params=params, timeout=40, allow_redirects=False)
        item = {"path": url, "principal": role, "status": response.status_code, "headers": dict(response.headers), "body": response.content}
        record["calls"].append(item)
        _save(path, record)
        assert response.status_code == status, item
        if feature:
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "content-disposition" not in response.headers
        assert int(response.headers["content-length"]) == len(response.content)
        if binary is not None:
            assert response.content == binary and response.headers["content-type"] == mime
        if code is not None:
            body = response.json()
            assert body["code"] == code and response.headers["content-type"] == "application/json"
            if status != 200 and feature:
                messages = {
                    400: "Invalid image request.",
                    401: "Unauthorized",
                    403: "Unauthorized",
                    404: "Image is unavailable.",
                    415: "Image data is invalid.",
                    422: "Invalid image request.",
                    500: "Image could not be read.",
                }
                assert body == {"code": code, "message": messages[status], "data": None}
        return response

    def image(key: str, role: str = "owner", kb: str = "kb", **kwargs: Any) -> requests.Response:
        return get(f"/api/v1/documents/images/{ids[kb]}-{quote(key, safe='')}", role, **kwargs)

    def deny_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rejection reached private object/index service")

    with monkeypatch.context() as guard:
        for target, attr in [
            (FileService, "parse"),
            (FileService, "parse_docs"),
            (FileService, "upload_info"),
            (FileService, "upload_infos"),
            (tempfile, "NamedTemporaryFile"),
            (tempfile, "mkstemp"),
            (tempfile, "mkdtemp"),
        ]:
            guard.setattr(target, attr, forbid)
        for target, attr in [
            (env["storage"], "put"),
            (env["storage"], "rm"),
            (env["adapter"], "put"),
            (settings.docStoreConn, "insert"),
            (settings.docStoreConn, "update"),
            (settings.docStoreConn, "delete"),
            (REDIS_CONN, "queue_product"),
        ]:
            guard.setattr(target, attr, forbid)
        sa.event.listen(env["async_engine"].sync_engine, "before_cursor_execute", sql_guard)
        sa.event.listen(env["engine"], "before_cursor_execute", sql_guard)
        try:
            with monkeypatch.context() as denied:
                denied.setattr(env["storage"], "get_bytes", deny_read)
                denied.setattr(settings.docStoreConn, "search", deny_read)
                for role in [None, "unknown", "expired", "malformed", "disabled"]:
                    for url in [
                        "/api/v1/thumbnails?doc_ids=" + env["manifest"]["documents"]["legacy"],
                        f"/api/v1/documents/images/{ids['kb']}-legacy.png",
                        f"/api/v1/documents/runtime/{setup['runtimes']['owner']['id']}/image",
                    ]:
                        get(url, role, status=401, code=401)
                with monkeypatch.context() as sdk_disabled:
                    sdk_disabled.setenv("DISABLE_SDK", "1")
                    image("other.png", "api", "foreign", status=401, code=401)
                for role in ["invite", "inactive", "outsider", "other"]:
                    image("legacy.png", role, status=404, code=102)
                for kb in ["foreign", "inactivekb"]:
                    image("other.png", kb=kb, status=404, code=102)
                denied.setattr(DocumentService, "get_thumbnails", deny_read)
                for role in [None, "owner", "malformed"]:
                    reads_before = len(sql_reads)
                    get("/v1/document/thumbnails", role, status=404, code=404, feature=False)
                    assert len(sql_reads) == reads_before
            docs = env["manifest"]["documents"]
            first = next(iter(setup["cases"]))
            params = [("doc_ids", docs[key]) for key in ["none", first, first, "foreign", "inactivekb", "inline", "emptythumbnail"]] + [("doc_ids", uuid4().hex)]
            mapping = get("/api/v1/thumbnails", params=params, code=0).json()
            expected = {docs["none"]: None, docs[first]: f"/api/v1/documents/images/{ids['kb']}-{quote(first, safe='')}", docs["inline"]: "data:image/webp;base64,abc", docs["emptythumbnail"]: ""}
            assert mapping == {"code": 0, "message": "success", "data": expected} and list(mapping["data"]) == list(expected)
            for role in ["invite", "inactive", "outsider", "other"]:
                assert get("/api/v1/thumbnails", role, params={"doc_ids": docs["legacy"]}, code=0).json()["data"] == {}
            get("/api/v1/thumbnails", status=422, code=101)
            get("/api/v1/thumbnails", params={"doc_ids": ""}, status=400, code=101)
            assert get("/api/v1/thumbnails", params=[("doc_ids", docs["legacy"])] * 100, code=0).json()["data"] == {docs["legacy"]: f"/api/v1/documents/images/{ids['kb']}-legacy.png"}
            get("/api/v1/thumbnails", params=[("doc_ids", docs["legacy"])] * 101, status=400, code=101)
            for role in ["owner", "normal", "admin"]:
                for key, (data, mime) in setup["cases"].items():
                    image(key, role, binary=data if mime else None, mime=mime, status=200 if mime else 415, code=None if mime else 102)
            image("other.png", "api", "foreign", binary=_image("PNG"), mime="image/png")
            for key in ["chunk-key.png", "child.png"]:
                image(key, binary=_image("PNG"), mime="image/png")
            for key in ["foreign-doc.png", "unregistered.png", "missing-object", "unknown-key"]:
                image(key, status=404, code=102)
            image("other.png", kb="sibling", binary=_image("PNG"), mime="image/png")
            image("legacy.png", kb="sibling", status=404, code=102)
            for bad in ["missinghyphen", "-key", ids["kb"] + "-"]:
                get("/api/v1/documents/images/" + bad, status=400, code=101)
            for method, url, kwargs in [
                ("get", f"/api/v1/datasets/{ids['kb']}/documents", {}),
                ("get", "/v1/document/list", {"params": {"kb_id": ids["kb"]}}),
                ("post", "/v1/document/list", {"params": {"id": ids["kb"]}, "json": {}}),
            ]:
                response = requests.request(method, env["base"] + url, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30, **kwargs)
                assert response.status_code == 200 and response.json()["code"] == 0, response.content
                listed = response.json()["data"]["docs"]
                thumb = next(item["thumbnail"] for item in listed if item["id"] == docs[first])
                assert thumb == expected[docs[first]]
                record["calls"].append({"producer": url, "body": response.content, "thumbnail": thumb})
                get(thumb, binary=_image("PNG"), mime="image/png")
            for role, token_role in [("owner", "owner"), ("other", "api")]:
                get(f"/api/v1/documents/runtime/{setup['runtimes'][role]['id']}/image", token_role, binary=_image("PNG"), mime="image/png")
            other_url = f"/api/v1/documents/runtime/{setup['runtimes']['other']['id']}/image"
            get(other_url, params={"owner": ids["other"], "created_by": ids["other"], "preview_url": "http://untrusted.invalid"}, status=404, code=102)
            for kind in ["tampered", "size", "empty", "missing-sidecar", "bad-json"]:
                get(f"/api/v1/documents/runtime/{setup['runtimes'][kind]['id']}/image", status=415 if kind in {"size", "empty"} else 404, code=102)
            for bad in ["bad", "A" * 32, "a" * 31]:
                get(f"/api/v1/documents/runtime/{bad}/image", status=400, code=101)
            get(f"/api/v1/documents/runtime/{uuid4().hex}/image", status=404, code=102)
            with monkeypatch.context() as fault:
                fault.setattr(env["adapter"], "conn", Minio("127.0.0.1:1", access_key="scratch", secret_key="scratch-secret", secure=False, http_client=urllib3.PoolManager(retries=0, timeout=1)))
                image("legacy.png", status=500, code=500)
            for kind in ["denied", "read", "close"]:
                with monkeypatch.context() as fault:
                    original_get = env["client"].get_object

                    def fault_get(*args: Any, **kwargs: Any) -> Any:
                        if kind == "denied":
                            raise S3Error("AccessDenied", "private object", "private", "request", "host", None)
                        raw = original_get(*args, **kwargs)

                        def bad_read() -> bytes:
                            raise OSError("private read failure")

                        def bad_close() -> None:
                            raw.close()
                            raise OSError("private close failure")

                        return SimpleNamespace(read=bad_read if kind == "read" else raw.read, close=bad_close if kind == "close" else raw.close, release_conn=raw.release_conn)

                    fault.setattr(env["client"], "get_object", fault_get)
                    image("legacy.png", status=500, code=500)
            with monkeypatch.context() as fault:
                fault.setitem(resources._state, "storage", setup["encrypted"])
                image("encrypted.png", binary=_image("PNG"), mime="image/png")
                image("broken.png", status=500, code=500)
            with monkeypatch.context() as fault:

                def unexpected(*args: Any, **kwargs: Any) -> Any:
                    raise ValueError("private index transport")

                fault.setattr(settings.docStoreConn, "search", unexpected)
                image("chunk-key.png", status=500, code=500)
            paths = get("/openapi.json", feature=False).json()["paths"]
            assert "/v1/document/thumbnails" not in paths and not any(path.startswith("/v1/document/image") for path in paths)
            assert "/v1/document/run" not in paths and "/v1/document/upload_and_parse" not in paths and "/v1/document/change_status" not in paths
            assert "/v1/document/change_parser" not in paths
            for canonical in ["/api/v1/thumbnails", "/api/v1/documents/images/{image_id}", "/api/v1/documents/runtime/{file_id}/image", "/api/v1/documents/ingest"]:
                assert canonical in paths
            record["openapi"] = paths
        finally:
            sa.event.remove(env["async_engine"].sync_engine, "before_cursor_execute", sql_guard)
            sa.event.remove(env["engine"], "before_cursor_execute", sql_guard)
    after = record["after"] = _snapshot(env)
    record.update(unchanged=before == after, write_attempts=guards)
    _save(path, record)
    assert not guards and record["unchanged"]
    runtime_ids = {item["id"] for item in setup["runtimes"].values()}
    assert not runtime_ids.intersection(row["id"] for row in before["sql"][Document.__tablename__])
    assert all(not runtime_ids.intersection(row["doc_id"] for row in index["rows"]) for index in before["index"].values())
    smoke = subprocess.run(smoke_command(), env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    path.with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_file_download_strict_reads_and_zero_bytes(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Real file route, SQL provenance, native objects and independent readback."""
    from sqlalchemy.orm import Session

    from api.db.db_models import File, File2Document
    from common.constants import FileSource
    from tests.support.document_image_read_service import _snapshot

    env, ids = image_http_api, image_http_api["ids"]
    _setup(env)
    kinds = ["zero", "fallback", "fallbackzero", "missing", "denied"]
    files = {kind: uuid4().hex for kind in kinds}
    docs = {kind: uuid4().hex for kind in kinds}
    links = {kind: uuid4().hex for kind in kinds}
    root = env["manifest"]["file_ids"][0]
    env["manifest"]["file_ids"].extend(files.values())
    env["manifest"]["documents"].update({"file-download-" + key: value for key, value in docs.items()})
    env["manifest"]["file_download_links"] = links
    env["register"]()
    with Session(env["engine"]) as db:
        for kind in kinds:
            owner = ids["other"] if kind == "denied" else ids["owner"]
            db.add(File(id=files[kind], parent_id=root, tenant_id=owner, created_by=owner, name=kind + ".txt", type="doc", location=kind, source_type=FileSource.KNOWLEDGEBASE))
            db.add(Document(id=docs[kind], kb_id=ids["foreign"] if kind == "denied" else ids["kb"], created_by=owner, name=kind + ".txt", type="doc", parser_id="naive", location="download-" + kind))
            db.add(File2Document(id=links[kind], file_id=files[kind], document_id=docs[kind]))
        db.commit()
    for namespace, key, data in [(root, "zero", b""), (ids["kb"], "download-fallback", b"fallback"), (ids["kb"], "download-fallbackzero", b"")]:
        env["storage"].put(namespace, key, data)
    before = _snapshot(env)
    evidence: dict[str, Any] = {"before": before, "calls": [], "reads": [], "writes": []}
    path = env["evidence"] / f"{ids['kb']}.file-download.json"
    _save(path, evidence)
    original = env["storage"].get_bytes

    def read(namespace: str, key: str) -> bytes | None:
        evidence["reads"].append([namespace, key])
        return original(namespace, key)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        evidence["writes"].append("forbidden storage or queue operation")
        raise AssertionError("download reached a forbidden operation")

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            evidence["writes"].append(statement)
            raise AssertionError("download attempted SQL mutation")

    def download(kind: str, role: str | None = "owner") -> requests.Response:
        headers = {"Authorization": "Bearer " + env["tokens"][role]} if role else {}
        response = requests.get(env["base"] + "/api/v1/files/" + files[kind], headers=headers, timeout=30)
        evidence["calls"].append({"kind": kind, "role": role, "status": response.status_code, "body": response.content, "headers": dict(response.headers)})
        _save(path, evidence)
        return response

    with monkeypatch.context() as guard:
        guard.setattr(env["storage"], "get_bytes", read)
        for target, name in [(env["storage"], "get"), (env["storage"], "put"), (env["storage"], "rm"), (REDIS_CONN, "queue_product")]:
            guard.setattr(target, name, forbid)
        for engine in [env["engine"], env["async_engine"].sync_engine]:
            sa.event.listen(engine, "before_cursor_execute", sql_guard)
        try:
            # The image fixture registers its malformed JWT as a valid API key.
            # Ordinary files deliberately accept API keys, so use an unknown key.
            for role in [None, "unknown"]:
                response = download("zero", role)
                assert response.status_code == 401 and response.json()["retcode"] == 109
            for kind, role in [("zero", "other"), ("denied", "owner")]:
                response = download(kind, role)
                assert response.status_code == 200 and response.json()["code"] == 102
            assert evidence["reads"] == []
            for kind, data, reads in [("zero", b"", 1), ("fallback", b"fallback", 2), ("fallbackzero", b"", 2)]:
                evidence["reads"].clear()
                response = download(kind)
                assert response.status_code == 200 and response.content == data
                assert "attachment" in response.headers["content-disposition"] and len(evidence["reads"]) == reads
            evidence["reads"].clear()
            response = download("missing")
            assert response.status_code == 404 and response.json() == {"code": 102, "message": "File not found in storage", "data": None}
            assert len(evidence["reads"]) == 2

            def unavailable(*args: Any, **kwargs: Any) -> Any:
                raise OSError("private transport detail")

            with monkeypatch.context() as fault:
                fault.setattr(env["adapter"].conn, "get_object", unavailable)
                response = download("zero")
                assert response.status_code == 503 and response.json() == {"code": 500, "message": "File could not be read", "data": None}
        finally:
            for engine in [env["engine"], env["async_engine"].sync_engine]:
                sa.event.remove(engine, "before_cursor_execute", sql_guard)
    evidence["after"] = _snapshot(env)
    evidence["unchanged"] = before == evidence["after"]
    _save(path, evidence)
    assert evidence["unchanged"] and evidence["writes"] == []
