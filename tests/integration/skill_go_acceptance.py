"""Opt-in Go native Skills with Python-owned scratch SQL, real MinIO/Milvus."""

import hashlib
import io
import json
import os
import subprocess
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import File, SkillIndexGeneration, SkillSpace, SkillVersionFile
from common import settings
from common.config_utils import CONFIGS
from tests.support.skills_http import bootstrapped_engine as bootstrapped_engine
from tests.support.skills_http import image_http_api as image_http_api
from tests.support.skills_http import image_http_database as image_http_database
from tests.support.skills_http import image_resources as image_resources
from tests.support.skills_http import skill_http as skill_http
from tests.support.skills_http import skill_request


@pytest.fixture
def go_skills(skill_http: dict[str, Any], tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = skill_http
    if algorithm := getattr(request, "param", None):
        from api.skills.storage import SkillStorage
        from common import resources
        from core.utils.encrypted_storage import EncryptedStorageWrapper

        source = env["storage"]
        plain = source.storage_impl if isinstance(source, EncryptedStorageWrapper) else source
        encrypted = EncryptedStorageWrapper(plain, algorithm=algorithm, key="skills-fixture-key")
        monkeypatch.setitem(resources._state, "storage", encrypted)
        monkeypatch.setitem(env, "storage", encrypted)
        monkeypatch.setattr(env["skill_worker"], "storage", SkillStorage(encrypted))
        monkeypatch.setenv("MultiRAG_CRYPTO_ENABLED", "true")
        monkeypatch.setenv("MultiRAG_CRYPTO_ALGORITHM", algorithm)
        monkeypatch.setenv("MultiRAG_CRYPTO_KEY", "skills-fixture-key")
    url = env["engine"].url
    mv, mn = CONFIGS["milvus"], CONFIGS["minio"]
    config = {
        "Database": {"Driver": "postgres", "Host": url.host, "Port": url.port, "Database": url.database, "Username": url.username, "Password": url.password, "Schema": "usr_ai"},
        "SecretKey": settings.SECRET_KEY,
        "Milvus": {"Address": mv["hosts"], "Username": mv.get("username", ""), "Password": mv.get("password", ""), "DBName": mv.get("db_name") or "default"},
        "Minio": {"Host": mn["host"], "User": mn["user"], "Password": mn["password"], "Secure": mn.get("secure", False), "Bucket": env["bucket"], "PrefixPath": "image-read"},
    }
    config_path, output_path = tmp_path / "config.json", tmp_path / "go.log"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    executable = os.environ.get("MULTIRAG_TEST_GO", "go")
    with output_path.open("w") as output:
        process = subprocess.Popen(
            [executable, "test", "./internal/skills", "-run", "^TestSkillsLiveServer$", "-count=1", "-v"],
            env={**os.environ, "MULTIRAG_SKILLS_LIVE_CONFIG": str(config_path)},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while not (tmp_path / "base").exists() and time.monotonic() < deadline:
                assert process.poll() is None, output_path.read_text()
                time.sleep(0.1)
            assert (tmp_path / "base").exists(), output_path.read_text()
            yield {**env, "python_base": env["base"], "base": (tmp_path / "base").read_text()}
        finally:
            (tmp_path / "stop").touch()
            try:
                result = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
                raise
            config_path.unlink()
            assert result == 0, output_path.read_text()


def complete(env: dict[str, Any], accepted: dict[str, Any], state: str = "succeeded") -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        op = skill_request(env, "GET", "/operations/" + accepted["operation_id"])
        if op["state"] in {"succeeded", "failed", "partial"}:
            assert op["state"] == state, json.dumps(op)
            return op
        time.sleep(0.2)
    raise AssertionError("Go operation did not complete")


def install(env: dict[str, Any], space: str, name: str, version: str = "1.0.0", activate: bool = True, key: str | None = None, expected: int = 202) -> tuple[dict[str, Any], dict[str, bytes]]:
    files = {"SKILL.md": f"---\nname: {name}\ndescription: {name} description\ntags: [fruit]\n---\n{name} content".encode(), "nested/asset.bin": b"\x00\xff\x01"}
    manifest = {
        "name": name,
        "version": version,
        "activate": activate,
        "files": [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in sorted(files.items())],
    }
    accepted = skill_request(
        env,
        "POST",
        f"/spaces/{space}/versions",
        files=[("file", (path, data)) for path, data in sorted(files.items())],
        data={"manifest": json.dumps(manifest)},
        key=key or uuid4().hex,
        expected=expected,
    )
    return accepted, files


def test_go_skill_assets_shared_storage_search_and_delete(go_skills: dict[str, Any]) -> None:
    env = go_skills
    py = {**env, "base": env["python_base"]}
    space = skill_request(env, "POST", "/spaces", body={"name": "Go native " + uuid4().hex})
    sid = space["id"]
    assert space["backend_owner"] == "go"
    assert skill_request(py, "GET", "/spaces/" + sid)["id"] == sid
    assert skill_request(py, "PATCH", "/spaces/" + sid, body={"name": "blocked", "revision": 1}, expected=409)["error_code"] == "BACKEND_OWNER_MISMATCH"
    assert skill_request(env, "GET", "/spaces/" + sid, token=env["tokens"]["outsider"], expected=404)["error_code"] == "NOT_FOUND"
    assert skill_request(env, "GET", "/spaces/" + sid, token=env["skill_api_token"])["id"] == sid
    accepted, files = install(env, sid, "orange")
    op = complete(env, accepted)
    assert op["result"]["index_state"] == "unindexed"
    skill, version = op["result"]["skill_id"], op["result"]["version_id"]
    downloaded = skill_request(py, "GET", f"/spaces/{sid}/versions/{version}/download")
    with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
        assert {path: archive.read(path) for path in archive.namelist()} == files
    assert skill_request(env, "POST", f"/spaces/{sid}/search", body={"query": "orange"}, expected=503)["error_code"] == "INDEX_NOT_READY"
    config = skill_request(env, "GET", f"/spaces/{sid}/config")
    configured = skill_request(
        env, "PATCH", f"/spaces/{sid}/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0], "rerank_model_id": env["skill_models"][2], "similarity_threshold": 0}
    )
    assert configured["requires_reindex"]
    complete(env, skill_request(env, "POST", f"/spaces/{sid}/reindex", key=uuid4().hex, expected=202))
    for backend in (env, py):
        for mode in ("keyword", "vector", "hybrid"):
            results = skill_request(backend, "POST", f"/spaces/{sid}/search", body={"query": "orange", "mode": mode})
            assert results["skills"][0]["version_id"] == version
            assert results["skills"][0]["score"] == 0.95
    env["skill_faults"]["rerank"] = True
    try:
        assert skill_request(env, "POST", f"/spaces/{sid}/search", body={"query": "orange"}, expected=503)["error_code"] == "RERANK_FAILED"
    finally:
        env["skill_faults"]["rerank"] = False
    # A real SQL failure during per-hit visibility filtering must not become an empty success.
    with env["engine"].begin() as connection:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_skills RENAME COLUMN active_version_id TO unavailable_active_version"))
    try:
        assert skill_request(env, "POST", f"/spaces/{sid}/search", body={"query": "orange"}, expected=503)["error_code"] == "SERVICE_UNAVAILABLE"
    finally:
        with env["engine"].begin() as connection:
            connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_skills RENAME COLUMN unavailable_active_version TO active_version_id"))
    with Session(env["engine"]) as db:
        generation = db.get(SkillSpace, sid).active_generation_id
        index_name = db.get(SkillIndexGeneration, generation).index_name
        bindings = list(db.scalars(sa.select(SkillVersionFile).where(SkillVersionFile.version_id == version)))
        addresses = [(db.get(File, item.file_id).parent_id, db.get(File, item.file_id).location) for item in bindings]
    assert env["skill_reader"].query(index_name, filter=f'version_id == "{version}"', output_fields=["version_id"])
    complete(env, skill_request(env, "DELETE", f"/spaces/{sid}/skills/{skill}", key=uuid4().hex, expected=202))
    assert skill_request(py, "GET", f"/spaces/{sid}/skills")["total"] == 0
    assert not env["skill_reader"].query(index_name, filter=f'version_id == "{version}"', output_fields=["version_id"], consistency_level="Strong")
    assert all(env["storage"].get_bytes(bucket, key) is None for bucket, key in addresses)
    complete(env, skill_request(env, "DELETE", f"/spaces/{sid}", key=uuid4().hex, expected=202))
    assert not env["skill_reader"].has_collection(index_name)


def test_go_skills_cli_consumer(go_skills: dict[str, Any], tmp_path: Path) -> None:
    from tests.support.skills_cli import exercise_skills_cli

    sid = exercise_skills_cli(go_skills, tmp_path)
    with Session(go_skills["engine"]) as db:
        assert db.get(SkillSpace, sid).state == "deleted"
        indexes = list(db.scalars(sa.select(SkillIndexGeneration.index_name).where(SkillIndexGeneration.space_id == sid)))
    assert all(not go_skills["skill_reader"].has_collection(index) for index in indexes)


def test_go_resume_published_operation_does_not_republish(go_skills: dict[str, Any]) -> None:
    from api.db.db_models import SkillOperation

    env = go_skills
    space = skill_request(env, "POST", "/spaces", body={"name": "Recovery " + uuid4().hex})
    sid = space["id"]
    config = skill_request(env, "GET", f"/spaces/{sid}/config")
    skill_request(env, "PATCH", f"/spaces/{sid}/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0]})
    accepted, _ = install(env, sid, "recover")
    completed = complete(env, accepted)
    with Session(env["engine"]) as db:
        before = db.get(SkillSpace, sid).active_generation_id
        original_calls = len(env["skill_calls"])
        db.execute(
            sa.update(SkillOperation)
            .where(SkillOperation.id == accepted["operation_id"])
            .values(state="running", phase="cleaning", lease_owner="crashed", lease_expires_at=sa.func.now() - sa.text("INTERVAL '1 minute'"))
        )
        db.commit()
    recovered = complete(env, accepted)
    assert recovered["attempts"] == completed["attempts"] + 1
    assert len(env["skill_calls"]) == original_calls
    with Session(env["engine"]) as db:
        assert db.get(SkillSpace, sid).active_generation_id == before
    complete(env, skill_request(env, "DELETE", f"/spaces/{sid}", key=uuid4().hex, expected=202))


def test_go_provider_failure_retry_keeps_old_generation(go_skills: dict[str, Any]) -> None:
    env = go_skills
    sid = skill_request(env, "POST", "/spaces", body={"name": "Retry " + uuid4().hex})["id"]
    config = skill_request(env, "GET", f"/spaces/{sid}/config")
    skill_request(env, "PATCH", f"/spaces/{sid}/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0]})
    accepted, _ = install(env, sid, "orange")
    complete(env, accepted)
    before = skill_request(env, "GET", f"/spaces/{sid}")["active_generation_id"]
    env["skill_faults"]["embedding"] = True
    try:
        accepted = skill_request(env, "POST", f"/spaces/{sid}/reindex", key="failing-reindex", expected=202)
        complete(env, accepted, "failed")
        assert skill_request(env, "GET", f"/spaces/{sid}")["active_generation_id"] == before
        replay = skill_request(env, "POST", f"/spaces/{sid}/reindex", key="failing-reindex", expected=202)
        assert replay["operation_id"] == accepted["operation_id"] and replay["state"] == "failed"
    finally:
        env["skill_faults"]["embedding"] = False
    retried = skill_request(env, "POST", "/operations/" + accepted["operation_id"] + "/retry", expected=202)
    complete(env, retried)
    assert skill_request(env, "GET", f"/spaces/{sid}")["active_generation_id"] != before
    complete(env, skill_request(env, "DELETE", f"/spaces/{sid}", key=uuid4().hex, expected=202))


@pytest.mark.parametrize("go_skills", ["aes-128-cbc", "aes-256-cbc"], indirect=True)
def test_go_python_encrypted_object_cross_read(go_skills: dict[str, Any]) -> None:
    from tests.support.skills_http import skill_complete

    env = go_skills
    py = {**env, "base": env["python_base"]}
    go_space = skill_request(env, "POST", "/spaces", body={"name": "encrypted Go " + uuid4().hex})["id"]
    accepted, files = install(env, go_space, "encrypted")
    done = complete(env, accepted)
    downloaded = skill_request(py, "GET", f"/spaces/{go_space}/versions/{done['result']['version_id']}/download")
    with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
        assert {path: archive.read(path) for path in archive.namelist()} == files
    with Session(env["engine"]) as db:
        binding = db.scalar(sa.select(SkillVersionFile).where(SkillVersionFile.version_id == done["result"]["version_id"]))
        file = db.get(File, binding.file_id)
        key = f"image-read/{file.parent_id}/{file.location}"
    raw = env["client"].get_object(env["bucket"], key)
    try:
        assert raw.read().startswith(b"RAGF")
    finally:
        raw.close()
        raw.release_conn()
    python_space = skill_request(py, "POST", "/spaces", body={"name": "encrypted Python " + uuid4().hex})["id"]
    config = skill_request(py, "GET", f"/spaces/{python_space}/config")
    skill_request(py, "PATCH", f"/spaces/{python_space}/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0], "similarity_threshold": 0})
    accepted, files = install(py, python_space, "python-encrypted")
    done = skill_complete(py, accepted)
    downloaded = skill_request(env, "GET", f"/spaces/{python_space}/versions/{done['result']['version_id']}/download")
    with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
        assert {path: archive.read(path) for path in archive.namelist()} == files
    for mode in ("keyword", "vector", "hybrid"):
        python_hits = skill_request(py, "POST", f"/spaces/{python_space}/search", body={"query": "python", "mode": mode})
        go_hits = skill_request(env, "POST", f"/spaces/{python_space}/search", body={"query": "python", "mode": mode})
        assert go_hits["skills"][0]["version_id"] == done["result"]["version_id"]
        assert go_hits["skills"][0]["score"] == pytest.approx(python_hits["skills"][0]["score"])
    complete(env, skill_request(env, "DELETE", f"/spaces/{go_space}", key=uuid4().hex, expected=202))
    skill_complete(py, skill_request(py, "DELETE", f"/spaces/{python_space}", key=uuid4().hex, expected=202))


def test_go_content_dedup_alias_and_mixed_owner_batch(go_skills: dict[str, Any]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from tests.support.skills_http import skill_complete

    env = go_skills
    sid = skill_request(env, "POST", "/spaces", body={"name": "Alias " + uuid4().hex})["id"]
    accepted, _ = install(env, sid, "alias", key="primary-key")
    complete(env, accepted)
    with ThreadPoolExecutor(max_workers=3) as executor:
        aliases = list(executor.map(lambda i: install(env, sid, "alias", key=f"alias-{i}")[0], range(3)))
    assert all(item["operation_id"] == accepted["operation_id"] for item in aliases)
    assert install(env, sid, "alias", version="2.0.0", key="alias-1", expected=409)[0]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert install(env, sid, "alias", activate=False, key="different-intent", expected=409)[0]["error_code"] == "VERSION_ALREADY_INSTALLED"
    py = {**env, "base": env["python_base"]}
    foreign = skill_request(py, "POST", "/spaces", body={"name": "Python owner " + uuid4().hex})["id"]
    batch = skill_request(env, "POST", "/spaces/delete", body={"ids": [sid, foreign]}, key=uuid4().hex, expected=202)
    result = complete(env, batch, "partial")
    items = {item["id"]: item for item in result["result"]["items"]}
    assert items[sid]["state"] == "succeeded"
    assert items[foreign]["error_code"] == "BACKEND_OWNER_MISMATCH"
    retried = skill_request(env, "POST", "/operations/" + batch["operation_id"] + "/retry", expected=202)
    assert len(complete(env, retried, "partial")["result"]["items"]) == 2
    assert skill_request(py, "GET", "/spaces/" + foreign)["state"] == "active"
    skill_complete(py, skill_request(py, "DELETE", "/spaces/" + foreign, key=uuid4().hex, expected=202))


def test_go_staging_crash_cleans_exact_objects(go_skills: dict[str, Any]) -> None:
    from api.db.db_models import Skill, SkillOperation, SkillVersion

    env = go_skills
    sid = skill_request(env, "POST", "/spaces", body={"name": "Staging " + uuid4().hex})["id"]
    accepted, _ = install(env, sid, "staging", activate=False)
    done = complete(env, accepted)
    version = done["result"]["version_id"]
    with Session(env["engine"]) as db:
        operation = db.get(SkillOperation, accepted["operation_id"])
        addresses = [(item["bucket"], item["key"]) for item in operation.payload["objects"]]
        blocked_file = operation.payload["objects"][0]["file_id"]
        assert len(blocked_file) == 32 and all(c in "0123456789abcdef" for c in blocked_file)
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.skills_cleanup_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.id = '{blocked_file}' THEN RAISE EXCEPTION 'injected cleanup failure'; END IF; RETURN OLD; END $$"
            )
        )
        db.execute(sa.text("CREATE TRIGGER skills_cleanup_fault BEFORE DELETE ON usr_ai.t_ai_files FOR EACH ROW EXECUTE FUNCTION usr_ai.skills_cleanup_fault()"))
        db.execute(sa.update(SkillVersion).where(SkillVersion.id == version).values(state="staging"))
        db.execute(sa.update(Skill).where(Skill.id == done["result"]["skill_id"]).values(active_version_id=None))
        db.execute(sa.update(SkillOperation).where(SkillOperation.id == operation.id).values(state="pending", phase="staging", update_date=sa.func.now() - sa.text("INTERVAL '2 hours'")))
        db.commit()
    failed = complete(env, accepted, "failed")
    assert failed["error"]["error_code"] == "OPERATION_FAILED"
    assert failed["error"]["retryable"] is True
    with Session(env["engine"]) as db:
        assert db.get(SkillVersion, version).state != "deleted"
        db.execute(sa.text("DROP TRIGGER skills_cleanup_fault ON usr_ai.t_ai_files"))
        db.execute(sa.text("DROP FUNCTION usr_ai.skills_cleanup_fault()"))
        db.commit()
    retried = skill_request(env, "POST", "/operations/" + accepted["operation_id"] + "/retry", expected=202)
    failed = complete(env, retried, "failed")
    assert failed["error"]["error_code"] == "INCOMPLETE_UPLOAD"
    assert failed["error"]["retryable"] is False
    assert skill_request(env, "POST", "/operations/" + accepted["operation_id"] + "/retry", expected=409)["error_code"] == "INCOMPLETE_UPLOAD"
    with Session(env["engine"]) as db:
        assert db.get(SkillVersion, version).state == "deleted"
        assert db.scalar(sa.select(sa.func.count()).select_from(SkillVersionFile).where(SkillVersionFile.version_id == version)) == 0
    assert all(env["storage"].get_bytes(bucket, key) is None for bucket, key in addresses)
    complete(env, skill_request(env, "DELETE", f"/spaces/{sid}", key=uuid4().hex, expected=202))


def test_go_python_asset_pagination_over_100(go_skills: dict[str, Any]) -> None:
    """Metadata-only seed tests SQL pagination, not upload validation or object storage."""
    from api.db.db_models import Skill, SkillVersion

    env = go_skills
    py = {**env, "base": env["python_base"]}
    sid = skill_request(env, "POST", "/spaces", body={"name": "Pagination " + uuid4().hex})["id"]
    foreign_space = uuid4().hex
    foreign_tenant = env["ids"]["outsider"]
    expected_ids: list[str] = []
    hidden_id = ""
    foreign_id = ""
    try:
        with Session(env["engine"]) as db:
            db.add(
                SkillSpace(
                    id=foreign_space,
                    tenant_id=foreign_tenant,
                    created_by=foreign_tenant,
                    name="Foreign pagination",
                    name_key="foreign pagination",
                    root_folder_id=uuid4().hex,
                    state="active",
                    backend_owner="go",
                    revision=1,
                )
            )
            db.flush()
            for space_id, tenant, count in ((sid, env["ids"]["owner"], 102), (foreign_space, foreign_tenant, 1)):
                skills: list[Skill] = []
                versions: list[SkillVersion] = []
                for index in range(count):
                    skill_id, version_id = uuid4().hex, uuid4().hex
                    deleting = space_id == sid and index == 101
                    skills.append(
                        Skill(
                            id=skill_id,
                            tenant_id=tenant,
                            space_id=space_id,
                            folder_id=uuid4().hex,
                            name=f"asset-{index:03}",
                            description="Pagination seed",
                            tags=[],
                            state="deleting" if deleting else "active",
                            revision=1,
                        )
                    )
                    # Zero-file installed versions are schema-valid isolated seeds. They
                    # deliberately do not exercise SKILL.md validation or downloads.
                    versions.append(
                        SkillVersion(
                            id=version_id,
                            tenant_id=tenant,
                            skill_id=skill_id,
                            folder_id=uuid4().hex,
                            version="1.0.0",
                            content_digest=hashlib.sha256(b"").hexdigest(),
                            manifest={"files": []},
                            source_kind="local",
                            state="installed",
                            index_state="unindexed",
                            file_count=0,
                            total_size=0,
                        )
                    )
                    if space_id == foreign_space:
                        foreign_id = skill_id
                    elif deleting:
                        hidden_id = skill_id
                    else:
                        expected_ids.append(skill_id)
                db.add_all(skills)
                db.flush()
                db.add_all(versions)
                db.flush()
                for skill, version in zip(skills, versions, strict=True):
                    skill.active_version_id = version.id
            db.commit()
        assert len(expected_ids) == 101
        assert skill_request(env, "GET", f"/spaces/{sid}/config")["top_k"] == 10
        for backend in (env, py):
            for search in (False, True):
                received: list[str] = []
                for page, length in ((1, 100), (2, 1), (3, 0)):
                    if search:
                        result = skill_request(backend, "POST", f"/spaces/{sid}/search", body={"query": "", "mode": "keyword", "page": page, "page_size": 100})
                        assert result["total_relation"] == "eq"
                        ids = [item["skill_id"] for item in result["skills"]]
                    else:
                        result = skill_request(backend, "GET", f"/spaces/{sid}/skills?page={page}&page_size=100&sort=name&desc=false")
                        assert (result["page"], result["page_size"]) == (page, 100)
                        ids = [item["id"] for item in result["skills"]]
                    assert result["total"] == 101
                    assert len(ids) == length
                    assert hidden_id not in ids and foreign_id not in ids
                    received.extend(ids)
                assert received == expected_ids
                assert len(set(received)) == 101
            assert skill_request(backend, "GET", f"/spaces/{foreign_space}/skills", expected=404)["error_code"] == "NOT_FOUND"
            assert skill_request(backend, "POST", f"/spaces/{sid}/search", token=env["tokens"]["outsider"], body={"query": "", "mode": "keyword"}, expected=404)["error_code"] == "NOT_FOUND"
    finally:
        # The shared fixture cleans its owner tenant; remove only this extra seed.
        with Session(env["engine"]) as db:
            db.execute(sa.update(Skill).where(Skill.space_id == foreign_space).values(active_version_id=None))
            db.execute(sa.delete(SkillVersion).where(SkillVersion.skill_id.in_(sa.select(Skill.id).where(Skill.space_id == foreign_space))))
            db.execute(sa.delete(Skill).where(Skill.space_id == foreign_space))
            db.execute(sa.delete(SkillSpace).where(SkillSpace.id == foreign_space))
            db.commit()
