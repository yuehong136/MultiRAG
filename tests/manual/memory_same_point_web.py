"""Read the restored operation and withdrawal through the pinned Web consumer."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from tests.manual.memory_compaction_rollout import ROOT, _owned_database, _serve_smoke
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file


def _read_retirement_and_withdrawal(checkout: Path, config: dict[str, Any], journal: ResourceJournal, owned: list[Path]) -> None:
    path = Path(os.environ["TMPDIR"]) / "same-point-retired-web.json"
    retired = {
        **config,
        "artifactId": config["retiredArtifactId"],
        "operationId": config["retiredOperationId"],
        "sourceIds": config.get("retiredSourceIds", config["sourceIds"]),
        "mode": "read-retired",
        "ownedScratchConfirmed": True,
    }
    private_consumer_file(path, json.dumps(retired), journal, owned)
    script = ROOT / "tests/support/memory_compaction_retirement_consumer.mts"
    completed = subprocess.run(
        ["node", "--import", "tsx", str(script), str(path)],
        cwd=checkout,
        env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    receipt = result["receipt"]
    assert receipt["passed"] is True and receipt["readOnlyRecovery"] is True and receipt["retiredAt"]
    assert receipt["artifactId"] == retired["artifactId"] and receipt["operationId"] == retired["operationId"]
    assert receipt["retirementRequests"] == receipt["generationRequests"] == receipt["applicationRequests"] == 0
    assert result["requests"] and all(row["method"] == "GET" for row in result["requests"])
    memory_id, source_id = config["memoryId"], config["withdrawnSourceId"]
    source_path = f"/api/v1/memories/{memory_id}/sources/{source_id}"
    with httpx.Client(base_url=config["baseURL"], headers={"Authorization": f"Bearer {config['token']}"}, trust_env=False, timeout=10) as client:
        state = client.get(source_path + "/state")
        excerpt = client.get(source_path)
    assert state.status_code == 200
    source = state.json()["data"]
    assert source["managed"] is True and source["state"] == "forgotten" and str(source["source_id"]) == str(source_id) and int(source["generation"]) > 1
    assert excerpt.status_code == 404 and excerpt.json()["data"] is False
    journal.event(
        "same-point-web-retirement-and-withdrawal", retired_operation_id=retired["operationId"], client_read_requests=len(result["requests"]), withdrawn_source_id=source_id, excerpt_http_status=404
    )


def smoke_target(engine: Engine, bindings: dict[str, Any]) -> None:
    """Read the admitted empty-target restore through the fixed Web revision."""
    pin = json.loads((ROOT / ".github/consumer-web.json").read_text())["ref"]
    assert os.environ["MULTIRAG_ROLLOUT_WEB_SHA"] == pin
    _serve_smoke("restored", {**bindings, "database": _owned_database(engine)}, read_extra=_read_retirement_and_withdrawal)
