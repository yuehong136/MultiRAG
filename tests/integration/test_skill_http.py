"""JWT/API-key consumer contract with independent database/object/index evidence."""

import io
import json
import zipfile
from typing import Any

import requests
import sqlalchemy as sa

from api.db.db_models import File, SkillIndexGeneration, SkillSpace, SkillVersionFile
from tests.support.skills_http import bootstrapped_engine as bootstrapped_engine
from tests.support.skills_http import image_http_api as image_http_api
from tests.support.skills_http import image_http_database as image_http_database
from tests.support.skills_http import image_resources as image_resources
from tests.support.skills_http import skill_complete, skill_request
from tests.support.skills_http import skill_http as skill_http


def install(env: dict[str, Any], space: str, version: str, *, description: str = "orange version", activate: bool = True) -> dict[str, Any]:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", f"---\nname: orange\ndescription: {description}\ntags: [fruit]\n---\nOrange manual")
        archive.writestr("nested/中文.txt", "orange nested bytes")
        archive.writestr("image.bin", b"\xff\x00")
    return skill_request(
        env,
        "POST",
        f"/spaces/{space}/versions",
        data={"manifest": json.dumps({"name": "orange", "version": version, "activate": activate})},
        files={"archive": ("package.zip", output.getvalue(), "application/zip")},
        key="install-" + version,
        expected=202,
    )


def test_authenticated_skill_lifecycle_real_stores(skill_http: dict[str, Any]) -> None:
    env = skill_http
    response = requests.get(env["base"] + "/api/v1/skills/spaces", timeout=30)
    assert response.status_code == 401 and response.json()["data"]["error_code"] == "UNAUTHENTICATED"
    models = skill_request(env, "GET", "/models")["models"]
    assert {item["id"] for item in models} == set(env["skill_models"])
    assert all(item["available"] for item in models)
    space = skill_request(env, "POST", "/spaces", body={"name": "Skill HTTP"})
    base = "/spaces/" + space["id"]
    assert skill_request(env, "GET", base, token=env["skill_api_token"])["id"] == space["id"]
    assert skill_request(env, "GET", base, token=env["tokens"]["other"], expected=404)["error_code"] == "NOT_FOUND"
    accepted = install(env, space["id"], "1.0.0")
    completed = skill_complete(env, accepted)
    assert completed["result"]["index_state"] == "unindexed" and completed["result"]["skipped_binary_count"] == 1
    version_id, skill_id = completed["result"]["version_id"], completed["result"]["skill_id"]
    assert install(env, space["id"], "1.0.0")["operation_id"] == accepted["operation_id"]
    assert skill_request(env, "POST", base + "/search", body={"query": "orange", "mode": "keyword"}, expected=503)["error_code"] == "INDEX_NOT_READY"
    binary = skill_request(env, "GET", base + f"/versions/{version_id}/download")
    with zipfile.ZipFile(io.BytesIO(binary)) as archive:
        assert archive.read("nested/中文.txt") == b"orange nested bytes"
        assert archive.read("image.bin") == b"\xff\x00"
    with env["engine"].connect() as db:
        files = db.execute(sa.select(SkillVersionFile, File).join(File, File.id == SkillVersionFile.file_id).where(SkillVersionFile.version_id == version_id)).mappings().all()
        assert len(files) == 3
    # Protected managed files cannot bypass the skill lifecycle through /files.
    denied = requests.delete(env["base"] + "/api/v1/files", headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, json={"ids": [space["root_folder_id"]]}, timeout=30).json()
    assert denied["code"] == 102 and denied["data"]["success_count"] == 0
    config = skill_request(env, "GET", base + "/config")
    patch = skill_request(env, "PATCH", base + "/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0], "rerank_model_id": env["skill_models"][2]})
    assert patch["requires_reindex"]
    skill_complete(env, skill_request(env, "POST", base + "/reindex", key="reindex-two", expected=202))
    generation_one = skill_request(env, "GET", base)["active_generation_id"]
    for mode in ("keyword", "vector", "hybrid"):
        hits = skill_request(env, "POST", base + "/search", body={"query": "orange", "mode": mode})
        assert hits["skills"][0]["version_id"] == version_id and hits["skills"][0]["score"] == 0.95
    env["skill_faults"]["rerank"] = True
    assert skill_request(env, "POST", base + "/search", body={"query": "orange", "mode": "hybrid"}, expected=503)["error_code"] == "RERANK_INVALID"
    env["skill_faults"]["rerank"] = False
    second = skill_complete(env, install(env, space["id"], "2.0.0", description="new orange metadata"))
    current = skill_request(env, "GET", base + f"/skills/{skill_id}")
    assert current["skill"]["description"] == "new orange metadata"
    assert current["skill"]["active_version_id"] == second["result"]["version_id"]
    config = skill_request(env, "GET", base + "/config")
    skill_request(env, "PATCH", base + "/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][1]})
    skill_complete(env, skill_request(env, "POST", base + "/reindex", key="reindex-three", expected=202))
    with env["engine"].connect() as db:
        generation = (
            db.execute(sa.select(SkillIndexGeneration.__table__).join(SkillSpace, SkillSpace.active_generation_id == SkillIndexGeneration.id).where(SkillSpace.id == space["id"])).mappings().one()
        )
        assert generation["dimension"] == 3 and generation["config"]["embedding_model_id"] == env["skill_models"][1]
    assert not env["skill_reader"].has_collection("skill_" + generation_one)
    assert env["skill_calls"] and all(call["authorized"] for call in env["skill_calls"])
    assert any(call["path"].endswith("rerank") for call in env["skill_calls"])
    deleted = skill_request(env, "DELETE", base, key="delete-space", expected=202)
    skill_request(env, "GET", base, expected=404)
    skill_complete(env, deleted)
    with env["engine"].connect() as db:
        assert db.scalar(sa.select(SkillSpace.state).where(SkillSpace.id == space["id"])) == "deleted"
        assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.tenant_id == env["ids"]["owner"], File.source_type.in_(["skill_space", "skill", "skill_version", "skill_file"]))) == 0
        names = db.scalars(sa.select(SkillIndexGeneration.index_name).where(SkillIndexGeneration.space_id == space["id"])).all()
    assert all(not env["skill_reader"].has_collection(name) for name in names)
    assert not list(env["client"].list_objects(env["bucket"], recursive=True))


def test_runtime_worker_lifecycle_advances_http_operations(skill_http: dict[str, Any], monkeypatch: Any) -> None:
    import asyncio
    import time

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from api.db import db_models
    from api.skills import runtime

    env = skill_http
    sessions = async_sessionmaker(env["async_engine"], expire_on_commit=False)
    assert runtime._task is None and runtime._worker is None
    monkeypatch.setattr(db_models, "async_session_factory", sessions)
    monkeypatch.setattr(runtime, "_search", env["skill_runtime"])

    async def completed(accepted: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            operation = await asyncio.to_thread(skill_request, env, "GET", "/operations/" + accepted["operation_id"])
            if operation["state"] in {"succeeded", "failed", "partial"}:
                assert operation["state"] == "succeeded", operation
                return operation
            await asyncio.sleep(0.1)
        raise AssertionError("Background worker did not complete the HTTP operation")

    async def scenario() -> None:
        await runtime.start_skill_worker()
        task = runtime._task
        assert task is not None and not task.done()
        await runtime.start_skill_worker()
        assert runtime._task is task
        try:
            space = await asyncio.to_thread(skill_request, env, "POST", "/spaces", body={"name": "Lifecycle worker"})
            accepted = await asyncio.to_thread(install, env, space["id"], "1.0.0")
            operation = await completed(accepted)
            assert operation["result"]["index_state"] == "unindexed"
            detail = await asyncio.to_thread(skill_request, env, "GET", f"/spaces/{space['id']}/skills/{operation['result']['skill_id']}")
            assert detail["skill"]["active_version_id"] == operation["result"]["version_id"]
            deleted = await asyncio.to_thread(skill_request, env, "DELETE", "/spaces/" + space["id"], key="lifecycle-delete", expected=202)
            await completed(deleted)
            await asyncio.to_thread(skill_request, env, "GET", "/spaces/" + space["id"], expected=404)
            async with sessions() as db:
                assert await db.scalar(sa.select(SkillSpace.state).where(SkillSpace.id == space["id"])) == "deleted"
                assert (
                    await db.scalar(
                        sa.select(sa.func.count()).select_from(File).where(File.tenant_id == env["ids"]["owner"], File.source_type.in_(["skill_space", "skill", "skill_version", "skill_file"]))
                    )
                    == 0
                )
            assert not list(env["client"].list_objects(env["bucket"], recursive=True))
        finally:
            await runtime.stop_skill_worker()
            assert task.done() and runtime._task is None and runtime._worker is None
            await runtime.stop_skill_worker()

    asyncio.run(scenario())
