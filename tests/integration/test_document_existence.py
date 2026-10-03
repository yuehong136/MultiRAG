"""Batch existence follows committed SQL deletion, across datasets."""

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from api.db.db_models import Document, Knowledgebase
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService


async def test_existing_ids_follow_physical_deletion(bootstrapped_async_engine: AsyncEngine) -> None:
    ids = [uuid4().hex for _ in range(3)]
    async with AsyncSession(bootstrapped_async_engine) as db:
        try:
            for doc_id, status in zip(ids, ["1", "0", "1"]):
                db.add(Document(id=doc_id, kb_id=uuid4().hex, created_by="existence-test", parser_id="naive", name="test", type="txt", status=status))
            await db.commit()
            async with AsyncSession(bootstrapped_async_engine) as reader:
                assert await DocumentService.get_existing_ids_async(reader, [*ids, ids[0], "missing"]) == set(ids)
                assert await DocumentService.get_existing_ids_async(reader, []) == set()
            await db.execute(sa.delete(Document).where(Document.id == ids[0]))
            await db.commit()
            for _ in range(2):
                async with AsyncSession(bootstrapped_async_engine) as reader:
                    assert await DocumentService.get_existing_ids_async(reader, ids) == set(ids[1:])
        finally:
            await db.execute(sa.delete(Document).where(Document.id.in_(ids)))
            await db.commit()


async def test_dataset_existence_requires_live_sql_parent(bootstrapped_async_engine: AsyncEngine) -> None:
    ids = [uuid4().hex for _ in range(2)]
    async with AsyncSession(bootstrapped_async_engine) as db:
        try:
            for identifier, status in zip(ids, ["1", "0"]):
                db.add(Knowledgebase(id=identifier, tenant_id="existence-test", created_by="existence-test", name="summary", embd_id="controlled", parser_id="naive", status=status))
            await db.commit()
            assert await KnowledgebaseService.get_existing_ids_async(db, [*ids, ids[0], "missing"]) == {ids[0]}
            await db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == ids[0]))
            await db.commit()
            assert await KnowledgebaseService.get_existing_ids_async(db, ids) == set()
        finally:
            await db.rollback()
            await db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(ids)))
            await db.commit()
