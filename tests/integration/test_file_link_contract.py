"""File linking executes the real converter using only scratch database sessions."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import file_convert_service
from api.db.db_models import Document, File, File2Document, Knowledgebase
from tests.support.agent_update_release import release_api as release_api


def test_file_link_http_sql_preflight(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    root, leaf, denied_folder, foreign_leaf, dataset, additional_dataset, foreign_dataset = [uuid4().hex for _ in range(7)]
    owner, other = env["owners"][:2]

    @contextmanager
    def scratch_connection() -> Any:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(file_convert_service, "db_connection", scratch_connection)
    with Session(env["engine"]) as db:
        for identifier, tenant in ((dataset, owner), (additional_dataset, owner), (foreign_dataset, other)):
            db.add(Knowledgebase(id=identifier, tenant_id=tenant, created_by=tenant, name=identifier, embd_id="unused"))
        for identifier, parent, tenant, kind in ((root, "", owner, "folder"), (leaf, root, owner, "doc"), (denied_folder, "", other, "folder"), (foreign_leaf, root, other, "doc")):
            db.add(File(id=identifier, parent_id=parent, tenant_id=tenant, created_by=tenant, name=identifier + ".txt", type=kind, location=identifier))
        db.commit()

    def snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with Session(env["engine"]) as db:
            docs = [doc.to_dict() for doc in db.scalars(sa.select(Document).where(Document.kb_id.in_([dataset, additional_dataset, foreign_dataset])).order_by(Document.id))]
            links = [link.to_dict() for link in db.scalars(sa.select(File2Document).where(File2Document.file_id.in_([leaf, foreign_leaf])))]
            return docs, links

    def link(files: list[str], datasets: list[str], token: str | None = None, mode: str = "replace") -> Any:
        return env["client"].post(
            f"{env['base']}/api/v1/files/link-to-datasets",
            params={"mode": mode},
            json={"file_ids": files, "kb_ids": datasets},
            headers={"Authorization": f"Bearer {token or env['keys'][0]}"},
            timeout=30,
        )

    try:
        before = snapshot()
        for files, datasets in (([denied_folder], [dataset]), ([root], [dataset]), ([leaf], [foreign_dataset]), ([leaf, uuid4().hex], [dataset])):
            result = link(files, datasets)
            assert result.status_code == 200 and result.json().get("retcode", result.json().get("code")) != 0, result.text
            assert snapshot() == before
        assert link([leaf], [dataset], "invalid").status_code == 401
        retired = env["client"].post(f"{env['base']}/api/v1/file/convert", json={"file_ids": [leaf], "kb_ids": [dataset]}, timeout=30)
        assert retired.status_code == 404 and snapshot() == before
        paths = env["client"].get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
        assert "/api/v1/file/convert" not in paths and "/api/v1/files/link-to-datasets" in paths
        # Remove only this fixture's foreign child; the same owned folder can now link.
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File).where(File.id == foreign_leaf))
            db.commit()
        result = link([root, leaf, root], [dataset])
        assert result.status_code == 200 and result.json().get("retcode", result.json().get("code")) == 0 and result.json()["data"] is True, result.text
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            docs, links = snapshot()
            if len(docs) == len(links) == 1:
                break
            time.sleep(0.05)
        assert len(docs) == len(links) == 1
        assert docs[0]["kb_id"] == dataset and docs[0]["created_by"] == owner and docs[0]["location"] == leaf
        assert links[0]["file_id"] == leaf and links[0]["document_id"] == docs[0]["id"]
        first_doc = docs[0]
        assert link([leaf], [dataset, dataset]).json()["retcode"] == 0
        assert snapshot()[0] == [first_doc]
        assert link([leaf], [additional_dataset], mode="add").json()["retcode"] == 0
        deadline = time.monotonic() + 10
        while len(snapshot()[0]) != 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(snapshot()[0]) == 2 and first_doc in snapshot()[0]
        assert link([leaf], [dataset], mode="invalid").status_code == 422
        assert link([leaf], [dataset], mode="replace").json()["retcode"] == 0
        deadline = time.monotonic() + 10
        while len(snapshot()[0]) != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert snapshot()[0] == [first_doc]
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File2Document).where(File2Document.file_id.in_([leaf, foreign_leaf])))
            db.execute(sa.delete(Document).where(Document.kb_id.in_([dataset, additional_dataset, foreign_dataset])))
            db.execute(sa.delete(File).where(File.id.in_([root, leaf, denied_folder, foreign_leaf])))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_([dataset, additional_dataset, foreign_dataset])))
            db.commit()
        assert snapshot() == ([], [])
