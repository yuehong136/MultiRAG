"""Real fixed Web clients preserve Agent labels and partial dataset settings."""

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Knowledgebase, UserCanvas
from tests.support.agent_update_release import message_dsl
from tests.support.agent_update_release import release_api as release_api
from tests.support.database import scratch_database
from tests.support.dataset_management_http import management_api as management_api

pytestmark = pytest.mark.external_consumer


@pytest.fixture
def bootstrapped_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    evidence = Path(os.environ.get("AFTER4776_WEB_EVIDENCE", str(tmp_path)))
    with scratch_database(evidence / "databases") as engine:
        yield engine


def run_web(mode: str, base: str, token: str, evidence: Path, **values: str) -> dict[str, Any]:
    root = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    runner = root / "node_modules/.bin/tsx"
    assert runner.is_file(), "An installed fixed Web checkout is required"
    completed = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(Path(__file__).resolve().parents[1] / "support/after4776_web_consumer.mts")],
        cwd=root,
        env={**os.environ, "AFTER4776_WEB_MODE": mode, "AFTER4776_WEB_BASE": base, "AFTER4776_WEB_TOKEN": token, **values},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{mode}.log").write_text(completed.stdout + completed.stderr + f"\nexit={completed.returncode}\n")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["passed"] and result["mode"] == mode
    return result


def test_real_web_canvas_label_roundtrip(release_api: dict[str, Any], tmp_path: Path) -> None:
    env = release_api
    evidence = Path(os.environ.get("AFTER4776_WEB_EVIDENCE", str(tmp_path)))
    result = run_web("agent", env["base"], env["jwts"][0], evidence, AFTER4776_WEB_DSL=json.dumps(message_dsl("Web label")))
    with Session(env["engine"]) as db:
        row = db.get(UserCanvas, result["id"])
        assert row is not None
        assert (row.user_id, row.canvas_type, row.canvas_category, row.title) == (env["owners"][0], "", "agent_canvas", "Web label consumer")


def test_real_web_dataset_empty_avatar_and_metadata_omission(management_api: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = management_api

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db
            db.commit()

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_db)
    identifier = env["datasets"][0]
    with Session(env["engine"]) as db:
        row = db.get(Knowledgebase, identifier)
        assert row is not None
        row.parser_config = {**row.parser_config, "built_in_metadata": [{"key": "filename", "type": "string"}], "extension": {"preserve": False, "count": 0}}
        db.commit()
    evidence = Path(os.environ.get("AFTER4776_WEB_EVIDENCE", str(tmp_path)))
    run_web("dataset", env["base"], env["jwt"][0], evidence, AFTER4776_WEB_DATASET=identifier)
    with Session(env["engine"]) as db:
        row = db.get(Knowledgebase, identifier)
        assert row is not None and row.avatar == ""
        assert row.parser_config["built_in_metadata"] == [] and row.parser_config["chunk_token_num"] == 256
        assert row.parser_config["extension"] == {"preserve": False, "count": 0}
