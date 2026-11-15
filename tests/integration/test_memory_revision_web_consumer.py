"""Pinned Web client exercises owned memory revisions against the real HTTP app."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tests.integration.test_memory_authorization import memory_api as memory_api
from tests.integration.test_memory_revisions_http import revision_api as revision_api
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def test_pinned_web_memory_revision_client(revision_api: dict[str, Any], tmp_path: Path) -> None:
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-revision-web-consumer")
    configuration = tmp_path / "memory-consumer.private.json"
    output = tmp_path / "memory-consumer-result.json"
    owned: list[Path] = []
    private_consumer_file(
        configuration,
        json.dumps(
            {
                "baseURL": revision_api["base"],
                "token": revision_api["jwts"][1],
                "memoryId": revision_api["memory_id"],
                "derivedMessageId": "910",
                "sourceId": "900",
                "correctedContent": "A project uses port 8080. A separate fact remains.",
                "ownedScratchConfirmed": True,
                "resultFile": str(output),
            }
        ),
        journal,
        owned,
    )
    command = ["node", "--import", "tsx", "scripts/consumers/memory-revisions.ts", str(configuration)]
    journal.event("reserve-memory-web-consumer", web_sha=web_sha, memory_id=revision_api["memory_id"], result_file=str(output))
    try:
        completed = subprocess.run(command, cwd=checkout, env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"}, text=True, capture_output=True, timeout=180)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        journal.event("memory-web-consumer-complete", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("memory-web-private-config-cleanup", paths=[str(path) for path in owned], absent=True)
