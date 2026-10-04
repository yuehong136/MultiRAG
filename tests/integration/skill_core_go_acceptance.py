"""Go core owns its tables/index; Python fixture only supplies isolated infrastructure."""

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa

from api.db.db_models import APIToken, Tenant, TenantLLM, User, UserTenant
from common import settings
from common.config_utils import CONFIGS
from tests.support.database import scratch_database
from tests.support.skills_http import bootstrapped_engine as bootstrapped_engine
from tests.support.skills_http import image_http_api as image_http_api
from tests.support.skills_http import image_http_database as image_http_database
from tests.support.skills_http import image_resources as image_resources
from tests.support.skills_http import skill_http as skill_http


@pytest.fixture
def go_skill_core(skill_http: dict[str, Any], tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    with ExitStack() as cleanup:
        env = dict(skill_http)
        go_db = cleanup.enter_context(scratch_database(tmp_path / "go-db"))
        # Only public identity/model test seeds cross into the independent Go DB.
        # No File tree, Skills metadata, operation or index row is copied.
        with skill_http["engine"].connect() as source, go_db.begin() as destination:
            for model in (User, Tenant, UserTenant, APIToken, TenantLLM):
                rows = [dict(row) for row in source.execute(sa.select(model.__table__)).mappings()]
                if rows:
                    destination.execute(sa.insert(model.__table__), rows)
        env["engine"] = go_db
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
            "Minio": {"Host": mn["host"], "User": mn["user"], "Password": mn["password"], "Secure": mn.get("secure", False), "Bucket": env["bucket"], "PrefixPath": "go-core/" + uuid4().hex},
        }
        config_path, output_path = tmp_path / "config.json", tmp_path / "go.log"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        executable = os.environ.get("MULTIRAG_TEST_GO", "go")
        with output_path.open("w") as output:

            def start_process() -> subprocess.Popen[Any]:
                return subprocess.Popen(
                    (
                        [os.environ["MULTIRAG_SKILL_CORE_TEST_BINARY"], "-test.run=^TestSkillCoreLiveServer$", "-test.v"]
                        if os.environ.get("MULTIRAG_SKILL_CORE_TEST_BINARY")
                        else [executable, "test", "./internal/handler", "-run", "^TestSkillCoreLiveServer$", "-count=1", "-v"]
                    ),
                    env={**os.environ, "MULTIRAG_SKILL_CORE_CONFIG": str(config_path)},
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )

            def await_base() -> str:
                deadline = time.monotonic() + 120
                while not (tmp_path / "base").exists() and time.monotonic() < deadline:
                    assert process.poll() is None, output_path.read_text()
                    time.sleep(0.1)
                assert (tmp_path / "base").exists(), output_path.read_text()
                return (tmp_path / "base").read_text()

            process = start_process()

            def restart() -> str:
                nonlocal process
                (tmp_path / "stop").touch()
                assert process.wait(timeout=20) == 0, output_path.read_text()
                (tmp_path / "stop").unlink()
                (tmp_path / "base").unlink()
                process = start_process()
                return await_base()

            try:
                yield {**env, "python_base": env["base"], "base": await_base(), "fault_path": tmp_path / "fail-delete", "config_path": config_path, "restart": restart, "go_minio": config["Minio"]}
            finally:
                (tmp_path / "stop").touch()
                try:
                    result = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
                    raise
                # Clean only physical collections whose identity belongs to this
                # independent fixture, including UI-created and failed-test spaces.
                with go_db.connect() as connection:
                    owned = connection.execute(sa.text("SELECT tenant_id,id FROM usr_ai.t_ai_go_skill_spaces")).all()
                prefixes = ["gskill_" + hashlib.sha256(("skill_" + tenant + "_" + sid).encode()).hexdigest()[:16] + "_" for tenant, sid in owned]
                reader = env["skill_reader"]
                for collection in reader.list_collections():
                    if any(collection.startswith(prefix) for prefix in prefixes):
                        for alias in reader.list_aliases(collection_name=collection):
                            reader.drop_alias(alias)
                        reader.drop_collection(collection)
                config_path.unlink()
                assert result == 0, output_path.read_text()


def test_go_skill_core_http_cli(go_skill_core: dict[str, Any], tmp_path: Path) -> None:
    from tests.support.skill_core_contract import exercise_skill_core

    exercise_skill_core(go_skill_core, tmp_path)
    if handoff := os.environ.get("MULTIRAG_SKILL_CORE_UI_HANDOFF"):
        path = Path(handoff)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"base": go_skill_core["base"], "token": go_skill_core["skill_api_token"], "model_ids": go_skill_core["skill_models"]}))
        path.chmod(0o600)
        deadline = time.monotonic() + 1800
        while not path.with_suffix(".done").exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        assert path.with_suffix(".done").exists(), "UI handoff timed out"
        path.unlink()


def _core_call(env: dict[str, Any], method: str, path: str, **kwargs: Any) -> Any:
    response = requests.request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + env["skill_api_token"]}, timeout=180, **kwargs)
    response.raise_for_status()
    payload = response.json()
    assert payload["code"] == 0, payload
    return payload["data"]


def test_go_core_reindex_failure_and_durable_file_cleanup(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    call = lambda method, path, **kwargs: _core_call(env, method, path, **kwargs)
    space = call("POST", "/skill-core/spaces", json={"name": "Fault isolation"})
    sid = space["id"]
    config = {"space_id": sid, "embd_id": env["skill_models"][0], "vector_similarity_weight": 0, "similarity_threshold": 0, "top_k": 20}
    call("POST", "/skill-core/config", json=config)
    call("POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", ("orange/1.0.0/SKILL.md", b"---\nname: orange\n---\nOrange instructions"))])
    call("POST", "/skill-core/reindex", json={"space_id": sid})
    assert call("POST", "/skill-core/search", json={"space_id": sid, "query": "orange"})["total"] == 1
    env["skill_faults"]["embedding"] = True
    try:
        response = requests.post(env["base"] + "/api/v1/skill-core/reindex", headers={"Authorization": "Bearer " + env["skill_api_token"]}, json={"space_id": sid}, timeout=90)
        assert response.json()["code"] != 0
    finally:
        env["skill_faults"]["embedding"] = False
    assert call("POST", "/skill-core/search", json={"space_id": sid, "query": "orange"})["total"] == 1
    folder = call("GET", "/files", params={"parent_id": space["folder_id"]})["files"][0]["id"]
    env["fault_path"].touch()
    try:
        response = requests.delete(env["base"] + "/api/v1/files", headers={"Authorization": "Bearer " + env["skill_api_token"]}, json={"file_ids": [folder]}, timeout=90)
        assert response.json()["code"] != 0
        with env["engine"].connect() as db:
            state = db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid})
            assert state["pending_delete"]
            assert db.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_files WHERE parent_id=:id"), {"id": folder}) > 0
    finally:
        env["fault_path"].unlink()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with env["engine"].connect() as db:
            state = db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid})
        if state is None:
            break
        time.sleep(0.2)
    assert state is None
    assert call("POST", "/skill-core/search", json={"space_id": sid, "query": "orange"})["total"] == 0
    call("DELETE", "/skill-core/spaces/" + sid)


def test_go_core_directory_pagination_over_100(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    space = _core_call(env, "POST", "/skill-core/spaces", json={"name": "101 assets"})
    _core_call(env, "POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", (f"asset-{i:03d}/1.0.0/SKILL.md", f"---\nname: asset-{i:03d}\n---\nBody".encode())) for i in range(101)])
    pages = [_core_call(env, "GET", "/files", params={"parent_id": space["folder_id"], "page": page, "page_size": 100}) for page in (1, 2, 3)]
    assert [row["total"] for row in pages] == [101, 101, 101]
    assert [len(row["files"]) for row in pages] == [100, 1, 0]
    assert len({file["id"] for row in pages for file in row["files"]}) == 101
    assert _core_call(env, "POST", "/skill-core/search", json={"space_id": space["id"], "query": ""})["total"] == 0
    _core_call(env, "POST", "/skill-core/config", json={"space_id": space["id"], "embd_id": env["skill_models"][0], "top_k": 10})
    _core_call(env, "POST", "/skill-core/reindex", json={"space_id": space["id"]})
    results = [_core_call(env, "POST", "/skill-core/search", json={"space_id": space["id"], "query": "", "page": page, "page_size": 100, "sort_by": "name", "sort_order": "asc"}) for page in (1, 2, 3)]
    assert [row["total"] for row in results] == [101, 101, 101]
    assert [len(row["skills"]) for row in results] == [100, 1, 0]
    assert len({skill["skill_id"] for row in results for skill in row["skills"]}) == 101
    _core_call(env, "DELETE", "/skill-core/spaces/" + space["id"])


def test_go_core_requires_explicit_space_without_side_effects(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    with env["engine"].connect() as db:
        before = [db.scalar(sa.text("SELECT count(*) FROM usr_ai." + name)) for name in ("t_ai_go_skill_spaces", "t_ai_go_skill_search_configs", "t_ai_files")]
    for value in (None, "", "   "):
        for method, path in (("GET", "/config"), ("POST", "/config"), ("POST", "/search"), ("POST", "/index"), ("DELETE", "/index"), ("POST", "/reindex")):
            payload = {} if value is None else {"space_id": value}
            response = requests.request(
                method,
                env["base"] + "/api/v1/skill-core" + path,
                headers={"Authorization": "Bearer " + env["skill_api_token"]},
                params=payload if method in {"GET", "DELETE"} else None,
                json=payload if method == "POST" else None,
                timeout=30,
            )
            assert response.status_code == 400 and response.json()["data"]["error_code"] == "SPACE_REQUIRED", response.text
    response = requests.post(env["base"] + "/api/v1/skill-core/search", headers={"Authorization": "Bearer " + env["skill_api_token"]}, json={"space_id": "default", "query": "orange"}, timeout=30)
    assert response.status_code == 404
    with env["engine"].connect() as db:
        after = [db.scalar(sa.text("SELECT count(*) FROM usr_ai." + name)) for name in ("t_ai_go_skill_spaces", "t_ai_go_skill_search_configs", "t_ai_files")]
    assert before == after
    assert not env["skill_reader"].has_collection("skill_" + env["ids"]["owner"] + "_default")


def test_go_core_large_utf8_document_and_fragment_deletion(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    call = lambda method, path, **kwargs: _core_call(env, method, path, **kwargs)
    space = call("POST", "/skill-core/spaces", json={"name": "Large UTF8 document"})
    sid = space["id"]
    fields = {name: {"enabled": True, "weight": 1} for name in ("name", "tags", "description", "content")}
    call("POST", "/skill-core/config", json={"space_id": sid, "embd_id": env["skill_models"][0], "vector_similarity_weight": 0, "similarity_threshold": 0, "top_k": 20, "field_config": fields})
    header = ("---\nname: Large fruit\ndescription: " + "说明" * 12000 + "\ntags: [orange]\n---\n").encode()
    tail = b"\nuniquefragmenttailtoken\n"
    unit = "正文🍊 orange\n".encode()
    body = header + unit * ((5 * 1024 * 1024 - len(header) - len(tail)) // len(unit)) + tail
    assert 5 * 1024 * 1024 - len(unit) <= len(body) <= 5 * 1024 * 1024
    call("POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", ("large/1.0.0/SKILL.md", body))])
    folder = call("GET", "/files", params={"parent_id": space["folder_id"]})["files"][0]["id"]
    # Move/rename is explicitly outside this phase; no File/index mutation occurs.
    response = requests.post(env["base"] + "/api/v1/files/move", headers={"Authorization": "Bearer " + env["skill_api_token"]}, json={"src_file_ids": [folder], "new_name": "renamed"}, timeout=30)
    assert response.json()["code"] != 0 and "SKILL_CORE_MOVE_UNAVAILABLE" in response.json()["message"]
    result = call("POST", "/skill-core/reindex", json={"space_id": sid})
    assert result["indexed_count"] == 1 and result["failed_count"] == 0, result
    found = call("POST", "/skill-core/search", json={"space_id": sid, "query": "uniquefragmenttailtoken"})
    assert found["total"] == 1 and found["skills"][0]["skill_id"] == "large", found
    assert found["skills"][0]["description"] == "说明" * 12000
    reader = env["skill_reader"]
    alias = "skill_" + env["ids"]["owner"] + "_" + sid
    physical = reader.describe_alias(alias)["collection_name"]
    fragments = reader.query(physical, filter='doc_id == "large"', output_fields=["id", "kind", "text", "ordinal"], limit=16384, consistency_level="Strong")
    assert sum(row["kind"] == "head" for row in fragments) == 1
    texts = sorted((row for row in fragments if row["kind"] == "text"), key=lambda row: row["ordinal"])
    assert len(texts) > 600 and all(len(row["text"].encode()) <= 8192 for row in texts)
    assert body.decode() in "".join(row["text"] for row in texts)
    assert sum(row["kind"] == "meta" for row in fragments) > 1
    call("DELETE", "/skill-core/index", params={"space_id": sid, "skill_id": "large"})
    assert reader.query(physical, filter='doc_id == "large"', output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"] == 0
    assert call("GET", "/files", params={"parent_id": space["folder_id"]})["total"] == 1
    call("DELETE", "/skill-core/spaces/" + sid)


def test_go_core_unpublished_fragments_keep_old_keyword_result(go_skill_core: dict[str, Any]) -> None:
    result = subprocess.run(
        [os.environ.get("MULTIRAG_TEST_GO", "go"), "test", "./internal/engine", "-run", "^TestSkillMilvusLiveUnpublishedFragments$", "-count=1", "-v"],
        env={**os.environ, "MULTIRAG_SKILL_CORE_CONFIG": str(go_skill_core["config_path"])},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_go_core_upload_sql_failure_survives_restart_and_late_write(go_skill_core: dict[str, Any]) -> None:
    from io import BytesIO

    from minio.error import S3Error

    env = go_skill_core
    space = _core_call(env, "POST", "/skill-core/spaces", json={"name": "Upload recovery"})
    sid = space["id"]
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                "CREATE FUNCTION usr_ai.fail_skill_upload() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.name='SKILL.md' THEN RAISE EXCEPTION 'injected upload SQL failure'; END IF; RETURN NEW; END $$"
            )
        )
        db.execute(sa.text("CREATE TRIGGER fail_skill_upload BEFORE INSERT ON usr_ai.t_ai_files FOR EACH ROW EXECUTE FUNCTION usr_ai.fail_skill_upload()"))
    env["fault_path"].touch()
    try:
        response = requests.post(
            env["base"] + "/api/v1/files",
            headers={"Authorization": "Bearer " + env["skill_api_token"]},
            data={"parent_id": space["folder_id"]},
            files=[("file", ("orange/1.0.0/SKILL.md", b"Orange"))],
            timeout=30,
        )
        assert response.json()["code"] != 0
        with env["engine"].connect() as db:
            state = db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid})
        address = state.get("pending_upload") or state["upload_tombstones"][0]
        physical = env["go_minio"]["PrefixPath"] + "/" + address["bucket"] + "/" + address["key"]
        assert env["client"].stat_object(env["bucket"], physical).size == 6
        with env["engine"].begin() as db:
            db.execute(sa.text("DROP TRIGGER fail_skill_upload ON usr_ai.t_ai_files"))
            db.execute(sa.text("DROP FUNCTION usr_ai.fail_skill_upload()"))
        env["base"] = env["restart"]()
    finally:
        env["fault_path"].unlink(missing_ok=True)

    def await_removed() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                env["client"].stat_object(env["bucket"], physical)
            except S3Error as exc:
                assert exc.code in {"NoSuchKey", "NoSuchObject"}
                return
            time.sleep(0.2)
        pytest.fail("upload tombstone did not remove object")

    await_removed()
    # A provider can complete an old Put after a timeout. Persistent tombstones
    # repeatedly delete that exact unique address without touching claimed files.
    env["client"].put_object(env["bucket"], physical, BytesIO(b"late"), 4)
    await_removed()
    _core_call(env, "POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", ("orange/1.0.0/SKILL.md", b"claimed"))])
    with env["engine"].connect() as db:
        state = db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid})
        file = db.execute(sa.text("SELECT id FROM usr_ai.t_ai_files WHERE name='SKILL.md' AND tenant_id=:tenant"), {"tenant": env["ids"]["owner"]}).one()
    assert state["upload_tombstones"] and "pending_upload" not in state
    time.sleep(11)
    response = requests.get(env["base"] + "/api/v1/files/" + file.id, headers={"Authorization": "Bearer " + env["skill_api_token"]}, timeout=30)
    assert response.status_code == 200 and response.content == b"claimed"
    _core_call(env, "DELETE", "/skill-core/spaces/" + sid)


def test_go_core_pending_delete_never_acknowledges_another_request(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    call = lambda method, path, **kwargs: _core_call(env, method, path, **kwargs)
    space = call("POST", "/skill-core/spaces", json={"name": "Delete request isolation"})
    sid = space["id"]
    call("POST", "/skill-core/config", json={"space_id": sid, "embd_id": env["skill_models"][0], "top_k": 10})
    call("POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", (name + "/1.0.0/SKILL.md", b"Orange instructions")) for name in ("alpha", "beta")])
    folders = {row["name"]: row["id"] for row in call("GET", "/files", params={"parent_id": space["folder_id"]})["files"]}
    env["fault_path"].touch()
    headers = {"Authorization": "Bearer " + env["skill_api_token"]}
    try:
        first = requests.delete(env["base"] + "/api/v1/files", headers=headers, json={"file_ids": [folders["alpha"]]}, timeout=30)
        assert first.json()["code"] != 0
        second = requests.delete(env["base"] + "/api/v1/files", headers=headers, json={"file_ids": [folders["beta"]]}, timeout=30)
        assert second.json()["code"] != 0 and "SKILL_CLEANUP_PENDING" in second.json()["message"]
        with env["engine"].connect() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_files WHERE id=:id"), {"id": folders["beta"]}) == 1
    finally:
        env["fault_path"].unlink()
    call("DELETE", "/files", json={"file_ids": [folders["alpha"]]})
    call("DELETE", "/files", json={"file_ids": [folders["beta"]]})
    with env["engine"].connect() as db:
        assert db.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_files WHERE id IN (:alpha,:beta)"), folders) == 0
    call("DELETE", "/skill-core/spaces/" + sid)


def test_go_core_delete_config_sql_failure_keeps_recovery_plan(go_skill_core: dict[str, Any]) -> None:
    env = go_skill_core
    call = lambda method, path, **kwargs: _core_call(env, method, path, **kwargs)
    space = call("POST", "/skill-core/spaces", json={"name": "Config read failure"})
    sid = space["id"]
    call("POST", "/skill-core/config", json={"space_id": sid, "embd_id": env["skill_models"][0], "top_k": 10})
    call("POST", "/files", data={"parent_id": space["folder_id"]}, files=[("file", ("orange/1.0.0/SKILL.md", b"Orange"))])
    folder = call("GET", "/files", params={"parent_id": space["folder_id"]})["files"][0]["id"]
    with env["engine"].begin() as db:
        db.execute(sa.text("ALTER TABLE usr_ai.t_ai_go_skill_search_configs RENAME TO unavailable_core_configs"))
    try:
        response = requests.delete(env["base"] + "/api/v1/files", headers={"Authorization": "Bearer " + env["skill_api_token"]}, json={"file_ids": [folder]}, timeout=30)
        assert response.json()["code"] != 0
        with env["engine"].connect() as db:
            state = db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid})
            assert state["pending_delete"][0]["id"] == folder
            assert db.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_files WHERE id=:id"), {"id": folder}) == 0
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text("ALTER TABLE usr_ai.unavailable_core_configs RENAME TO t_ai_go_skill_search_configs"))
    # This is a retry of the original plan after its File ID was already removed.
    call("DELETE", "/files", json={"file_ids": [folder]})
    with env["engine"].connect() as db:
        assert db.scalar(sa.text("SELECT core_state FROM usr_ai.t_ai_go_skill_spaces WHERE id=:id"), {"id": sid}) is None
    call("DELETE", "/skill-core/spaces/" + sid)
