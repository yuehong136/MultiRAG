"""Sibling Web domain clients preserve list scope over the real HTTP boundary."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.db_models import Dialog, Search
from tests.support.dataset_management_http import management_api as management_api
from tests.support.web_consumer import tsx_runner


@pytest.mark.external_consumer
def test_actual_web_owner_list_clients(management_api: dict[str, Any]) -> None:
    env = management_api
    root = Path(os.environ["WEB_DATASET_CHECKOUT"])
    runner = tsx_runner(root)
    script = Path(__file__).resolve().parents[1] / "support/app_list_web_consumer.mts"
    assert runner.exists() and script.exists()
    ids: list[str] = []
    with env["engine"].begin() as db:
        for number, owner in enumerate(env["users"]):
            for item in range(2):
                for model in [Dialog, Search]:
                    identifier = uuid4().hex
                    ids.append(identifier)
                    extra = {"llm_id": ""} if model is Dialog else {"created_by": owner}
                    db.execute(sa.insert(model).values(id=identifier, tenant_id=owner, name=("Owner" if number == 0 else "Other") + f" App {item}", **extra))

    def snapshot() -> dict[str, Any]:
        with env["engine"].connect() as db:
            return {model.__tablename__: list(db.execute(sa.select(model.__table__).where(model.id.in_(ids)).order_by(model.id))) for model in [Dialog, Search]}

    before = snapshot()
    try:
        result = subprocess.run(
            [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "APP_LIST_BASE": env["base"], "APP_LIST_OWNERS": json.dumps(env["users"]), "APP_LIST_TOKENS": json.dumps([env["jwt"][2], env["keys"][2]])},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        assert evidence["passed"] is True and evidence["requests"] == 12
        env["record_path"].with_suffix(".web-list.json").write_text(json.dumps(evidence, indent=2))
        assert snapshot() == before
    finally:
        with env["engine"].begin() as db:
            for model in [Dialog, Search]:
                db.execute(sa.delete(model).where(model.id.in_(ids)))
        assert all(not rows for rows in snapshot().values())
