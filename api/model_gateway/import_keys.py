"""Preview or apply a digest-only caller key manifest as a database operator."""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.model_gateway.keys import hash_key
from api.model_gateway.schemas import GatewayKeyImportBatch


async def import_keys(db: AsyncSession, batch: GatewayKeyImportBatch, *, apply: bool, actor_id: str = "gateway-key-import") -> list[dict[str, Any]]:
    """Plan the whole batch before staging inserts. The caller owns the transaction."""
    from api.db.db_models import GatewayImportedCredential, GatewayKey, Tenant
    from api.model_gateway.management import build_key

    results: list[dict[str, Any]] = []
    pending: list[GatewayKey] = []
    credentials: list[GatewayImportedCredential] = []
    source_ids: set[str] = set()
    digests: set[str] = set()
    names: set[tuple[str, str]] = set()
    for item in batch.keys:
        live_name = (item.tenant_id, item.name)
        spellings = [item.sha256, *item.alternate_sha256]
        report: dict[str, Any] = {
            "source_id": item.source_id,
            "tenant_id": item.tenant_id,
            "name": item.name,
            "requested_policy": item.model_dump(mode="json", exclude={"sha256", "alternate_sha256", "description", "tags", "unresolved", "source_id", "tenant_id", "name"}),
            "credential_forms": len(spellings),
        }
        results.append(report)
        if item.unresolved:
            report["error"] = "unresolved_source_policy"
            report["unresolved"] = item.unresolved
        elif item.source_id in source_ids or len(set(spellings)) != len(spellings) or not digests.isdisjoint(spellings) or (item.status != "revoked" and live_name in names):
            report["error"] = "duplicate_in_manifest"
        source_ids.add(item.source_id)
        digests.update(spellings)
        if item.status != "revoked":
            names.add(live_name)
        if "error" in report:
            continue
        # Compare the original manifest, never replay it over later rotations or policy edits.
        fingerprint = hash_key(json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
        existing = await db.scalar(select(GatewayKey).where(GatewayKey.import_source == batch.source, GatewayKey.import_id == item.source_id))
        if existing is not None:
            if existing.import_spec_hash != fingerprint:
                report["error"] = "source_record_changed"
            else:
                report.update(action="already_imported", key_id=existing.id, current_status=existing.status)
            continue
        if await db.scalar(select(Tenant.id).where(Tenant.id == item.tenant_id)) is None:
            report["error"] = "unknown_tenant"
            continue
        if await db.scalar(select(GatewayImportedCredential.key_id).where(GatewayImportedCredential.digest.in_(spellings)).limit(1)) is not None:
            report["error"] = "credential_already_registered"
            continue
        if item.status != "revoked" and await db.scalar(select(GatewayKey.id).where(GatewayKey.tenant_id == item.tenant_id, GatewayKey.name == item.name, GatewayKey.status != "revoked")) is not None:
            report["error"] = "name_in_use"
            continue
        try:
            row = await build_key(db, item.tenant_id, actor_id, item, uuid.uuid4().hex, item.sha256)
        except ValueError as exc:
            report["error"] = str(exc)
            continue
        row.status = item.status
        row.import_digest, row.import_source, row.import_id, row.import_spec_hash = item.sha256, batch.source, item.source_id, fingerprint
        pending.append(row)
        credentials.extend(GatewayImportedCredential(key_id=row.id, digest=digest, allow_raw_authorization=item.allow_raw_authorization) for digest in spellings)
        report["action"] = "would_import"
    if apply and not any("error" in result for result in results):
        db.add_all(pending)
        await db.flush()
        db.add_all(credentials)
        await db.flush()
        inserted = {row.import_id: row.id for row in pending}
        for report in results:
            if report["action"] == "would_import":
                report.update(action="imported", key_id=inserted[report["source_id"]])
    return results


async def _run(batch: GatewayKeyImportBatch, *, apply: bool) -> int:
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    from api.db.db_models import async_session_factory

    if async_session_factory is None:
        raise RuntimeError("A PostgreSQL operator connection is required")
    async with async_session_factory() as db:
        results = await import_keys(db, batch, apply=apply)
        blocked = any("error" in result for result in results)
        if apply and not blocked:
            await db.commit()
        print(json.dumps({"applied": apply and not blocked, "results": results}, ensure_ascii=True))
        return int(blocked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="UTF-8 JSON manifest with SHA-256 digests of the exact bearer tokens")
    parser.add_argument("--apply", action="store_true", help="commit the entire batch if every entry passes")
    args = parser.parse_args()
    try:
        batch = GatewayKeyImportBatch.model_validate_json(args.manifest.read_bytes())
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
            return runner.run(_run(batch, apply=args.apply))
    except ValidationError as exc:
        # An operator may accidentally supply a plaintext field. Never echo input or unknown field names.
        for err in exc.errors(include_input=False, include_context=False):
            loc = err["loc"]
            row = loc[1] + 1 if len(loc) > 1 and isinstance(loc[1], int) else None
            print(f"Invalid manifest (row={row}, reason={err['type']})", file=sys.stderr)
    except OSError:
        print("Cannot read the manifest or reach the configured database.", file=sys.stderr)
    except IntegrityError:
        print("Import conflict. No entries committed. Preview again against the current database.", file=sys.stderr)
    except SQLAlchemyError:
        print("Database operation failed. Check the database connection and schema, then preview again.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
