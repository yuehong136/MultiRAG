"""Inspect or attest memory writer quiescence after an independently verified stop.

Run through scripts/run_registered.py with the configured environment and a private
evidence directory. This command never kills workers or certifies physical erasure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("action", choices=("audit", "attest-legacy", "attest-attempt"))
    command.add_argument("--memory-id", required=True)
    command.add_argument("--tenant-id", required=True)
    command.add_argument("--expected-generation", type=int)
    command.add_argument("--expected-task-set-sha256")
    command.add_argument("--operation-id")
    command.add_argument("--expected-attempt", type=int)
    command.add_argument("--expected-active-token")
    command.add_argument("--evidence-reference")
    command.add_argument("--confirm-all-selected-io-settled", action="store_true", help="Attest that selected processes and remote requests have independently settled")
    return command


async def execute(options: argparse.Namespace) -> dict[str, Any]:
    from sqlalchemy import select

    from api.db.db_models import async_db_connection
    from api.db.memory_writer_models import MemoryWriterAdmission
    from memory.writes.admission import acknowledge_legacy_quiescence, writer_drain_snapshot
    from memory.writes.operators import acknowledge_attempt_quiescence

    async with async_db_connection() as db:
        if options.action == "audit":
            result = dict(await writer_drain_snapshot(db, memory_id=options.memory_id, tenant_id=options.tenant_id))
            attempts = list(
                await db.execute(
                    select(MemoryWriterAdmission.operation_id, MemoryWriterAdmission.attempt).where(
                        MemoryWriterAdmission.memory_id == options.memory_id, MemoryWriterAdmission.tenant_id == options.tenant_id
                    )
                )
            )
            result["attempt_numbers"] = {row.operation_id: row.attempt for row in attempts}
            return result
        if options.action == "attest-legacy":
            return dict(
                await acknowledge_legacy_quiescence(
                    db,
                    memory_id=options.memory_id,
                    tenant_id=options.tenant_id,
                    expected_generation=options.expected_generation,
                    expected_untracked_task_ids_sha256=options.expected_task_set_sha256,
                    evidence_reference=options.evidence_reference,
                )
            )
        return await acknowledge_attempt_quiescence(
            db,
            memory_id=options.memory_id,
            tenant_id=options.tenant_id,
            operation_id=options.operation_id,
            expected_generation=options.expected_generation,
            expected_attempt=options.expected_attempt,
            expected_active_token=options.expected_active_token,
            evidence_reference=options.evidence_reference,
        )


def main(argv: list[str] | None = None) -> int:
    command = parser()
    options = command.parse_args(argv)
    if options.action != "audit":
        required = ["expected_generation", "evidence_reference"]
        required += ["expected_task_set_sha256"] if options.action == "attest-legacy" else ["operation_id", "expected_attempt", "expected_active_token"]
        if not options.confirm_all_selected_io_settled or any(getattr(options, name) is None for name in required):
            command.error("Attestation requires exact expected identities, evidence and --confirm-all-selected-io-settled")
    if not os.environ.get("MULTIRAG_RUN_ID"):
        command.error("Run this command through scripts/run_registered.py before importing the application")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print(json.dumps(asyncio.run(execute(options)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
