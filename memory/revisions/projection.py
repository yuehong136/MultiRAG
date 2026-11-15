"""Recoverable projection execution; SQL publication decisions remain in RevisionService."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionError, RevisionStorageUnknown, validate_projection
from memory.revisions.service import RevisionService

Embedder = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
PhysicalReader = Callable[[int], Awaitable[dict[str, Any] | None]]
PhysicalWriter = Callable[[dict[str, Any]], Awaitable[None]]
StatusWriter = Callable[[int, bool], Awaitable[None]]


async def reconcile_operation(
    service: RevisionService,
    memory_id: str,
    operation_id: str,
    *,
    embedder: Embedder,
    physical_reader: PhysicalReader,
    physical_writer: PhysicalWriter,
    status_writer: StatusWriter,
) -> dict[str, Any]:
    """Advance one durable operation, checking the same fence before each external step.

    An unknown remote write is never reissued during this attempt. A later
    explicit reconciliation first reads the stable physical ID to resolve it.
    """
    operation = await service.get_operation(memory_id, operation_id)
    if operation["status"] == "ready":
        return operation
    snapshot = await service.claim_publication(memory_id, operation_id)
    outbox_id, token = snapshot["outbox_id"], snapshot["lease_token"]
    if snapshot["state"] == "ready":
        return await service.get_operation(memory_id, operation_id)
    try:
        snapshot = await service.publication_snapshot(outbox_id, lease_token=token)
        payload = snapshot["payload"]
        expected = deepcopy(payload["message"])
        binding = ModelBinding(**payload["model_binding"])
        published = snapshot["state"] == "sync_pending" or payload.get("published") is True
        if not published:
            existing = await finish_owned_work(physical_reader(snapshot["message_id"]))
            if existing is None:
                embedded = await finish_owned_work(embedder(expected["content"], binding.to_dict()))
                if embedded.get("model_binding") != binding.to_dict():
                    raise RevisionConflict("model_binding_changed")
                staged = {**expected, "status": False, "content_embed": embedded["vector"]}
                validate_projection(staged, expected, binding, active=False)
                await service.publication_snapshot(outbox_id, lease_token=token)
                await finish_owned_work(physical_writer(staged))
                existing = await finish_owned_work(physical_reader(snapshot["message_id"]))
                if existing is None:
                    raise RevisionStorageUnknown("projection_write_not_visible")
            await service.record_stage(outbox_id, existing, lease_token=token)
            await service.publish_revision(outbox_id, lease_token=token)
        snapshot = await service.publication_snapshot(outbox_id, lease_token=token)
        current = await finish_owned_work(physical_reader(snapshot["message_id"]))
        if current is None:
            raise RevisionStorageUnknown("published_projection_missing")
        if current.get("status") not in (True, 1, "1"):
            await service.publication_snapshot(outbox_id, lease_token=token)
            await finish_owned_work(status_writer(snapshot["message_id"], True))
        old_id = snapshot["payload"]["old_message_id"]
        old = await finish_owned_work(physical_reader(old_id))
        if old is not None and old.get("status") not in (False, 0, "0"):
            await service.publication_snapshot(outbox_id, lease_token=token)
            await finish_owned_work(status_writer(old_id, False))
        active = await finish_owned_work(physical_reader(snapshot["message_id"]))
        old = await finish_owned_work(physical_reader(old_id))
        if active is None:
            raise RevisionStorageUnknown("published_projection_missing")
        return await service.mark_ready(outbox_id, active, old_message_inactive=old is None or old.get("status") in (False, 0, "0"), lease_token=token)
    except asyncio.CancelledError:
        try:
            await finish_owned_work(service.mark_unknown(outbox_id, "projection_interrupted", lease_token=token))
        except RevisionError:
            pass
        raise
    except RevisionConflict:
        try:
            return await service.fail_operation(outbox_id, "publication_fence_changed", stale=True, lease_token=token)
        except RevisionError:
            return await service.get_operation(memory_id, operation_id)
    except Exception:
        return await service.mark_unknown(outbox_id, "projection_io_unconfirmed", lease_token=token)
    finally:
        if token is not None:
            await finish_owned_work(service.finish_publication_attempt(outbox_id, lease_token=token))
