"""Pinned Web list clients through registered HTTP, auth and real SQL."""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.db_models import DocumentMetadata, File, File2Document
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_sql import SqlMetadataStore
from tests.support.dataset_management_http import management_api as management_api

pytestmark = pytest.mark.external_consumer


def _run_web(env: dict[str, Any], mode: str = "documents") -> dict[str, Any]:
    checkout = Path(os.environ["WEB_DATASET_CHECKOUT"])
    expected = json.loads(Path(".github/consumer-web.json").read_text())["ref"]
    observed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert observed == expected, "Test the accepted committed Web version"
    completed = subprocess.run(
        [str(checkout / "node_modules/.bin/tsx"), "--tsconfig", str(checkout / "tsconfig.app.json"), str(Path("tests/support/metadata_web_consumer.mts").resolve())],
        cwd=checkout,
        env={
            **os.environ,
            "METADATA_WEB_BASE": env["base"],
            "METADATA_WEB_TOKEN": env["jwt"][0],
            "METADATA_WEB_DATASET": env["datasets"][0],
            "METADATA_WEB_DOCUMENT": env["documents"][0],
            "METADATA_WEB_MODE": mode,
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return {**json.loads(completed.stdout.splitlines()[-1]), "web_commit": observed}


def test_real_web_document_metadata_zero_matches(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]) -> None:
    env = management_api
    dataset, document, owner = env["datasets"][0], env["documents"][0], env["users"][0]
    file_id, link_id = uuid4().hex, uuid4().hex
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    with env["engine"].begin() as db:
        db.execute(sa.insert(File).values(id=file_id, parent_id="", tenant_id=owner, created_by=owner, name="filter.txt", type="txt"))
        db.execute(sa.insert(File2Document).values(id=link_id, file_id=file_id, document_id=document))
        db.execute(sa.insert(DocumentMetadata).values(id=document, kb_id=dataset, tenant_id=owner, meta_fields={"score": 0, "approved": False}))
    try:
        result = _run_web(env)
        assert result["passed"] and len(result["responses"]) == 6
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(DocumentMetadata.meta_fields).where(DocumentMetadata.id == document)) == {"score": 0, "approved": False}
        record_property("web_metadata_readback", json.dumps({**result, "sql_unchanged": True}))
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == document))
            db.execute(sa.delete(File2Document).where(File2Document.id == link_id))
            db.execute(sa.delete(File).where(File.id == file_id))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(DocumentMetadata.id).where(DocumentMetadata.id == document)) is None
            assert db.scalar(sa.select(File2Document.id).where(File2Document.id == link_id)) is None
            assert db.scalar(sa.select(File.id).where(File.id == file_id)) is None
