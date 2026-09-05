"""Real SQL coverage for dataset reads and active tenant membership boundaries."""

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.apps.services import dataset_api_service
from api.db.db_models import Document, Knowledgebase, PipelineOperationLog, UserTenant
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID


@pytest.mark.parametrize(("role", "active", "allowed"), [("owner", "1", True), ("admin", "1", True), ("normal", "1", True), ("invite", "1", False), ("owner", "0", False)])
async def test_async_dataset_access_matches_sync_policy(bootstrapped_async_engine, role, active, allowed):
    uid, tid, kid = (uuid4().hex for _ in range(3))
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Knowledgebase(id=kid, name="access", tenant_id=tid, created_by=uid, embd_id="test"))
        db.add(UserTenant(id=uuid4().hex, tenant_id=tid, user_id=uid, role=role, status=active, invited_by=uid))
        await db.flush()
        assert await KnowledgebaseService.accessible_async(db, kid, uid) is allowed
        assert await db.run_sync(lambda s: KnowledgebaseService.accessible(s, kid, uid)) is allowed
        assert not await KnowledgebaseService.accessible_async(db, kid, "outsider")
        await db.rollback()


async def test_async_dataset_detail_summary_logs_and_scoping(bootstrapped_async_engine):
    uid, tid, kid, other = (uuid4().hex for _ in range(4))
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Knowledgebase(id=kid, name="read-contract", tenant_id=tid, created_by=uid, embd_id="test", doc_num=2, chunk_num=7, token_num=70))
        db.add(UserTenant(id=uuid4().hex, tenant_id=tid, user_id=uid, role="owner", invited_by=uid))
        for status in ["1", "3"]:
            db.add(Document(id=uuid4().hex, kb_id=kid, parser_id="naive", type="txt", created_by=uid, run=status))
        logs = []
        for kb_id, document_id, status in [
            (kid, GRAPH_RAPTOR_FAKE_DOC_ID, "success"),
            (kid, GRAPH_RAPTOR_FAKE_DOC_ID, "failed"),
            (kid, "document-only", "success"),
            (other, GRAPH_RAPTOR_FAKE_DOC_ID, "success"),
        ]:
            log = PipelineOperationLog(
                id=uuid4().hex,
                kb_id=kb_id,
                tenant_id=tid,
                document_id=document_id,
                parser_id="naive",
                document_name="test",
                document_suffix="txt",
                document_type="txt",
                source_from="local",
                operation_status=status,
            )
            logs.append(log)
            db.add(log)
        await db.flush()
        ok, detail = await dataset_api_service.get_dataset(db, uid, kid)
        assert ok and detail["id"] == kid and detail["name"] == "read-contract"
        ok, summary = await dataset_api_service.get_ingestion_summary(db, uid, kid)
        assert ok and summary["chunk_num"] == 7 and summary["status"]["running_count"] == 1 and summary["status"]["done_count"] == 1
        ok, listed = await dataset_api_service.list_ingestion_logs(
            db, uid, kid, page=1, page_size=1, operation_status=["success"], create_date_from=datetime(2000, 1, 1), create_date_to=datetime(2099, 1, 1)
        )
        assert ok and listed["total"] == 1 and listed["logs"][0]["id"] == logs[0].id
        ok, detail_log = await dataset_api_service.get_ingestion_log(db, uid, kid, logs[0].id)
        assert ok and detail_log == listed["logs"][0]
        assert await dataset_api_service.get_ingestion_log(db, uid, kid, logs[-1].id) == (False, "Log not found")
        assert await dataset_api_service.trace_index(db, uid, kid, "raptor") == (True, {})
        assert (await dataset_api_service.get_dataset(db, "outsider", kid))[0] is False
        await db.rollback()
