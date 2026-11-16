"""Python independent core contract against scratch SQL/MinIO/Milvus and CLI."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.support import event_loop
from tests.support.skills_http import (
    bootstrapped_engine as bootstrapped_engine,
)
from tests.support.skills_http import (
    image_http_api as image_http_api,
)
from tests.support.skills_http import (
    image_http_database as image_http_database,
)
from tests.support.skills_http import (
    image_resources as image_resources,
)
from tests.support.skills_http import (
    skill_http as skill_http,
)


def test_core_shared_http_cli_contract(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    if os.environ.get("SKILLS_API_PROTOCOL") != "ragflow-skills-v1":
        from tests.support.skill_core_contract import exercise_core_protocol_process

        exercise_core_protocol_process()
        return
    skill_http = request.getfixturevalue("skill_http")
    from api.apps import app
    from api.skills import core_runtime
    from api.skills.core_runtime import PythonCoreSearch, PythonCoreStore, get_core_search
    from tests.support.skill_core_contract import exercise_skill_core

    env = skill_http
    runtime = PythonCoreSearch(PythonCoreStore(env["skill_reader"]), env["skill_runtime"].model_runtime)
    monkeypatch.setitem(app.dependency_overrides, get_core_search, lambda: runtime)
    monkeypatch.setattr(core_runtime, "_search", runtime)
    sessions = async_sessionmaker(env["async_engine"], expire_on_commit=False)
    from tests.support.resource_journal import ResourceJournal

    journal = ResourceJournal("canonical-python-core-cli")
    from api.skills.core_schemas import CoreIndexRequest
    from api.skills.core_service import SkillCoreService

    actual_index = SkillCoreService.index

    async def observe_index(self: SkillCoreService, tenant: str, body: CoreIndexRequest, *, rebuild: bool = False) -> dict[str, Any]:
        result = await actual_index(self, tenant, body, rebuild=rebuild)
        journal.event("core-cli-real-index-response", space_id=body.space_id, requested_skills=len(body.skills), rebuild=rebuild, response=result)
        return result

    monkeypatch.setattr(SkillCoreService, "index", observe_index)
    names: list[str] = []
    create = runtime.store.create

    def tracked_create(name: str, dimension: int) -> None:
        journal.event("reserve-core-cli-index", name=name, dimension=dimension, database=env["engine"].url.database)
        names.append(name)
        create(name, dimension)

    monkeypatch.setattr(runtime.store, "create", tracked_create)
    from api.db import db_models

    monkeypatch.setattr(db_models, "async_session_factory", sessions)
    # The HTTP fixture disables application lifespan. Exercise the actual
    # start/stop lifecycle functions in its dedicated worker event loop.
    import threading

    stop = threading.Event()
    started = threading.Event()

    def background() -> None:
        async def run() -> None:
            await core_runtime.start_core_worker()
            started.set()
            await asyncio.to_thread(stop.wait)
            await core_runtime.stop_core_worker()

        event_loop.run(run())

    worker = threading.Thread(target=background, daemon=True)
    worker.start()
    assert started.wait(10)
    try:
        exercise_skill_core(env, tmp_path)
    finally:
        stop.set()
        worker.join(timeout=10)
        assert not worker.is_alive() and core_runtime._task is None
        for name in names:
            runtime.store.delete(name)
            assert not env["skill_reader"].has_collection(name)
            journal.event("core-cli-index-cleanup-readback", name=name, absent=True)


def test_core_requires_explicit_space_without_effects(skill_http: dict[str, Any]) -> None:
    import requests
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from api.db.db_models import File, PythonSkillCoreConfig, PythonSkillCoreSpace

    env = skill_http
    headers = {"Authorization": "Bearer " + env["skill_api_token"]}
    origin = env["base"] + "/api/v1/skill-core"
    tenant = env["ids"]["owner"]

    def snapshot() -> list[Any]:
        with Session(env["engine"]) as db:
            return [list(db.execute(sa.select(model.__table__).where(model.tenant_id == tenant)).all()) for model in (File, PythonSkillCoreSpace, PythonSkillCoreConfig)]

    model_response = requests.get(origin + "/models", headers=headers, timeout=15)
    assert model_response.status_code == 200 and model_response.headers["X-Skills-Protocol"] == "ragflow-skills-v1"
    assert env["skill_models"][0] in [model["id"] for model in model_response.json()["data"]["models"]]
    discovery = requests.get(env["base"] + "/api/v1/skill-protocols", headers=headers, timeout=15).json()["data"]
    assert discovery["default_protocol"] == "multirag-assets-v1"
    assert all(protocol["writable"] for protocol in discovery["protocols"])
    before = snapshot()
    for identity in (None, "", "  "):
        body = {} if identity is None else {"space_id": identity}
        for path in ("/config", "/search", "/index", "/reindex"):
            response = requests.post(origin + path, headers=headers, json=body, timeout=15)
            assert response.status_code == 400 and response.json()["data"]["error_code"] == "SPACE_REQUIRED", response.text
        for method, path in (("GET", "/config"), ("DELETE", "/index")):
            response = requests.request(method, origin + path, headers=headers, params={**body, "skill_id": "unknown"}, timeout=15)
            assert response.status_code == 400 and response.json()["data"]["error_code"] == "SPACE_REQUIRED", response.text
    response = requests.post(origin + "/search", headers=headers, json={"space_id": "default"}, timeout=15)
    assert response.status_code == 404
    assert snapshot() == before
    space = requests.post(origin + "/spaces", headers=headers, json={"name": "Authorization scope"}, timeout=15).json()["data"]
    before = snapshot()
    foreign_headers = {"Authorization": "Bearer " + env["tokens"]["other"]}
    for method, path, body in (
        ("GET", "/spaces/" + space["id"], None),
        ("POST", "/search", {"space_id": space["id"]}),
        ("POST", "/reindex", {"space_id": space["id"]}),
        ("DELETE", "/spaces/" + space["id"], None),
    ):
        response = requests.request(method, origin + path, headers=foreign_headers, json=body, timeout=15)
        assert response.status_code == 404, response.text
    assert snapshot() == before


def test_core_zero_vector_weight_matches_default_threshold_without_embedding(skill_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from api.apps import app
    from api.skills import core_runtime
    from api.skills.core_runtime import PythonCoreSearch, PythonCoreStore, get_core_search

    env = skill_http
    runtime = PythonCoreSearch(PythonCoreStore(env["skill_reader"]), env["skill_runtime"].model_runtime)
    monkeypatch.setitem(app.dependency_overrides, get_core_search, lambda: runtime)
    monkeypatch.setattr(core_runtime, "_search", runtime)
    names: list[str] = []
    create = runtime.store.create

    def tracked_create(name: str, dimension: int) -> None:
        names.append(name)
        create(name, dimension)

    monkeypatch.setattr(runtime.store, "create", tracked_create)
    headers = {"Authorization": "Bearer " + env["skill_api_token"]}

    def call(method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(method, env["base"] + "/api/v1" + path, headers=headers, timeout=60, **kwargs)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        return response.json()["data"]

    try:
        space = call("POST", "/skill-core/spaces", json={"name": "Keyword weights"})
        config = {"space_id": space["id"], "embd_id": env["skill_models"][0], "vector_similarity_weight": 0}
        assert call("POST", "/skill-core/config", json=config)["similarity_threshold"] == 0.2
        skill = call("POST", "/files", json={"parent_id": space["folder_id"], "name": "orange-ui", "type": "folder"})
        call("POST", "/files", data={"parent_id": skill["id"]}, files={"file": ("SKILL.md", b"---\nname: orange-ui\ndescription: orange UI acceptance\n---\norange")})
        assert call("POST", "/skill-core/reindex", json={"space_id": space["id"]})["indexed_count"] == 1
        calls = len(env["skill_calls"])
        result = call("POST", "/skill-core/search", json={"space_id": space["id"], "query": "orange"})
        assert result["search_type"] == "keyword" and result["total"] == 1
        assert result["skills"][0]["name"] == "orange-ui" and result["skills"][0]["score"] >= 0.2
        assert len(env["skill_calls"]) == calls
        call("POST", "/skill-core/config", json={**config, "vector_similarity_weight": 1})
        result = call("POST", "/skill-core/search", json={"space_id": space["id"], "query": "orange"})
        assert result["search_type"] == "vector" and result["total"] == 1
        assert len(env["skill_calls"]) > calls
    finally:
        for name in names:
            runtime.store.delete(name)
            assert not env["skill_reader"].has_collection(name)


@pytest.mark.parametrize("preexisting", [False, True])
def test_core_space_uses_one_owned_skills_namespace(skill_http: dict[str, Any], preexisting: bool) -> None:
    import requests
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from api.db.db_models import File, PythonSkillCoreSpace
    from tests.support.resource_journal import ResourceJournal

    env = skill_http
    owner = env["ids"]["owner"]
    headers = {"Authorization": "Bearer " + env["skill_api_token"]}
    journal = ResourceJournal("core-space-namespace")
    journal.event("reserve-core-space-http", database=env["engine"].url.database, tenant=owner, names=["first-space", "second-space"], preexisting=preexisting)
    namespace_id = None
    if preexisting:
        response = requests.post(env["base"] + "/api/v1/files", headers=headers, json={"name": "skills", "type": "folder"}, timeout=30)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        namespace_id = response.json()["data"]["id"]
    spaces = []
    for name in ("first-space", "second-space"):
        response = requests.post(env["base"] + "/api/v1/skill-core/spaces", headers=headers, json={"name": name}, timeout=30)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        spaces.append(response.json()["data"])
        # Read only after HTTP success, using a separate SQL connection.
        with Session(env["engine"]) as db:
            stored = db.get(PythonSkillCoreSpace, spaces[-1]["id"])
            assert stored is not None and stored.tenant_id == owner and stored.folder_id == spaces[-1]["folder_id"]
            directory = db.get(File, stored.folder_id)
            assert directory is not None and directory.type == "folder" and directory.tenant_id == owner and directory.name == name
            namespace = db.get(File, directory.parent_id)
            assert namespace is not None and namespace.name == "skills" and namespace.type == "folder" and namespace.tenant_id == owner
            assert namespace.source_type == ""
            root = db.get(File, namespace.parent_id)
            assert root is not None and root.id == root.parent_id and root.tenant_id == owner
            assert namespace_id in (None, namespace.id)
            namespace_id = namespace.id
            assert list(db.scalars(sa.select(File.id).where(File.tenant_id == owner, File.parent_id == root.id, File.name == "skills"))) == [namespace_id]
        fetched = requests.get(env["base"] + "/api/v1/skill-core/spaces/" + spaces[-1]["id"], headers=headers, timeout=30)
        assert fetched.status_code == 200 and fetched.json()["data"]["folder_id"] == spaces[-1]["folder_id"], fetched.text
    with Session(env["engine"]) as db:
        assert set(db.scalars(sa.select(File.id).where(File.tenant_id == owner, File.parent_id == namespace_id))) == {space["folder_id"] for space in spaces}
    journal.event("core-space-directory-readback", database=env["engine"].url.database, namespace=namespace_id, space_ids=[space["id"] for space in spaces], same_tenant_root=True)


@pytest.mark.parametrize("conflict", ["duplicate", "nonfolder", "managed", "child"])
def test_core_namespace_conflicts_and_foreign_mutations_have_zero_effects(skill_http: dict[str, Any], conflict: str) -> None:
    from uuid import uuid4

    import requests
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from api.db.db_models import File, PythonSkillCoreConfig, PythonSkillCoreSpace
    from tests.support.resource_journal import ResourceJournal

    env = skill_http
    owner, other = env["ids"]["owner"], env["ids"]["other"]
    headers = {"Authorization": "Bearer " + env["skill_api_token"]}
    journal = ResourceJournal("core-space-namespace-conflict")
    response = requests.post(env["base"] + "/api/v1/skill-core/spaces", headers=headers, json={"name": "owned-space"}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    space = response.json()["data"]

    def snapshot() -> dict[str, list[dict[str, Any]]]:
        with env["engine"].connect() as db:
            return {
                model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).where(model.tenant_id.in_([owner, other])).order_by(model.id)).mappings()]
                for model in (File, PythonSkillCoreSpace, PythonSkillCoreConfig)
            }

    before = snapshot()
    foreign_headers = {"Authorization": "Bearer " + env["tokens"]["other"]}
    response = requests.put(env["base"] + "/api/v1/skill-core/spaces/" + space["id"], headers=foreign_headers, json={"name": "foreign-rename"}, timeout=30)
    assert response.status_code == 404 and response.json()["data"]["error_code"] == "NOT_FOUND", response.text
    assert snapshot() == before
    with Session(env["engine"]) as db:
        namespace = db.get(File, db.get(File, space["folder_id"]).parent_id)
        assert namespace is not None
        identifier = uuid4().hex
        journal.event("reserve-core-namespace-conflict", database=env["engine"].url.database, tenant=owner, kind=conflict, namespace=namespace.id, extra_file_id=identifier)
        if conflict == "duplicate":
            db.add(File(id=identifier, tenant_id=owner, parent_id=namespace.parent_id, created_by=owner, name="skills", type="folder", source_type=""))
        elif conflict == "nonfolder":
            namespace.type = "other"
        elif conflict == "managed":
            namespace.source_type = "python_skill_space_core"
        else:
            db.add(File(id=identifier, tenant_id=owner, parent_id=namespace.id, created_by=owner, name="new-space", type="folder", source_type=""))
        db.commit()
    before = snapshot()
    response = requests.post(env["base"] + "/api/v1/skill-core/spaces", headers=headers, json={"name": "new-space"}, timeout=30)
    assert response.status_code == 409 and response.json()["data"]["error_code"] == "RESOURCE_CONFLICT", response.text
    assert snapshot() == before
    journal.event("core-namespace-rejections-readback", foreign_status=404, conflict_status=409, full_owned_sql_unchanged=True)
