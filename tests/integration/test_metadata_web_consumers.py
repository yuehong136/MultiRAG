"""Pinned Web list clients through registered HTTP, auth and real SQL."""

import json
import logging
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from elasticsearch import Elasticsearch
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import DocumentMetadata, File, File2Document, Knowledgebase
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_sql import SqlMetadataStore
from common import settings
from core.nlp import search
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
            "METADATA_WEB_DATASETS": json.dumps(env["datasets"]),
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


@pytest.mark.parametrize("backend", ["infinity", "elasticsearch"])
def test_real_web_tags_with_store_aggregations(management_api: dict[str, Any], backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_property: Callable[[str, object], None]) -> None:
    """Retain the existing tuple consumer and prove actual store/HTTP shapes."""
    env = management_api
    names = ["f_tags_" + uuid4().hex for _ in range(3)]
    owners = [env["users"][0], env["users"][0], env["users"][1]]
    indexes = [search.index_name_one(owner, name) for owner, name in zip(owners, names, strict=True)]
    records = [
        [{"id": "a1", "tag_kwd": ["shared", "only-a"]}, {"id": "a2", "tag_kwd": ["shared"]}],
        [{"id": "b1", "tag_kwd": ["shared", "only-b"]}],
        [{"id": "secret", "tag_kwd": ["secret"]}],
    ]
    for kb_id, name in zip(env["datasets"], names, strict=True):
        with env["engine"].begin() as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == kb_id).values(name=name))

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_db)
    monkeypatch.setattr(search, "db_connection", scratch_db)
    evidence = Path(os.environ.get("MULTIRAG_F_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"backend": backend, "indexes": indexes, "kbs": env["datasets"], "cleanup": []}
    path = evidence / ("tags_" + backend + "_" + uuid4().hex + ".json")
    with ExitStack() as resources:
        resources.callback(lambda: path.write_text(json.dumps(record, indent=2)))
        if backend == "infinity":
            import infinity
            from infinity.common import ConflictType, NetworkAddress
            from infinity.connection_pool import ConnectionPool

            from core.utils.infinity_conn import InfinityConnection

            host, port = os.environ["INFINITY_TEST_URI"].rsplit(":", 1)
            address = NetworkAddress(host, int(port))
            writer, reader = infinity.connect(address), infinity.connect(address)
            resources.callback(writer.disconnect)
            resources.callback(reader.disconnect)
            pool = ConnectionPool(address, max_size=4)
            database = "after9d_f_tags_" + uuid4().hex
            record.update(uri=f"{host}:{port}", database=database)

            def remove_database() -> None:
                writer.get_database("default_db")
                reader.get_database("default_db")
                writer.drop_database(database, ConflictType.Ignore)
                assert database not in reader.list_databases().db_names
                record["cleanup"].append({"database": database, "absent": True})

            resources.callback(remove_database)
            resources.callback(pool.destroy)
            db = writer.create_database(database, ConflictType.Error)
            factory = InfinityConnection
            cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
            connector = object.__new__(cls)
            connector.dbName, connector.connPool, connector.logger = database, pool, logging.getLogger(__name__)
            tables = [index + "_" + kb for index, kb in zip(indexes, env["datasets"], strict=True)]
            record["tables"] = tables
            for table, docs in zip(tables, records, strict=True):
                db.create_table(table, {"id": {"type": "varchar"}, "tag_kwd": {"type": "varchar"}}).insert([{**doc, "tag_kwd": "###".join(doc["tag_kwd"])} for doc in docs])

            def snapshot() -> Any:
                return {table: reader.get_database(database).get_table(table).output(["id", "tag_kwd"]).to_result()[0] for table in tables}

        else:
            from core.utils.es_conn import ESConnection

            url = os.environ["METADATA_TEST_ES_URL"]
            record["uri"] = url
            writer = resources.enter_context(Elasticsearch(url, request_timeout=15))
            reader = resources.enter_context(Elasticsearch(url, request_timeout=15))
            assert reader.info()["version"]["number"].startswith("8.")
            factory = ESConnection
            cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
            connector = object.__new__(cls)
            connector.es, connector.logger = writer, logging.getLogger(__name__)
            connector.mapping = json.loads(Path("configs/es_mapping.json").read_text())

            def remove_index(index: str) -> None:
                if reader.indices.exists(index=index):
                    writer.indices.delete(index=index)
                assert not reader.indices.exists(index=index)
                record["cleanup"].append({"index": index, "absent": True})

            for index, kb_id, docs in zip(indexes, env["datasets"], records, strict=True):
                resources.callback(remove_index, index)
                writer.indices.create(index=index, mappings={"properties": {"id": {"type": "keyword"}, "kb_id": {"type": "keyword"}, "tag_kwd": {"type": "keyword"}}})
                for doc in docs:
                    writer.index(index=index, id=doc["id"], document={**doc, "kb_id": kb_id}, refresh=True)

            def snapshot() -> Any:
                return {index: reader.search(index=index, sort="id")["hits"]["hits"] for index in indexes}

        monkeypatch.setattr(settings, "DOC_ENGINE", backend)
        dealer = object.__new__(search.Dealer)
        dealer.dataStore = connector
        monkeypatch.setitem(settings.__dict__, "retriever", dealer)
        calls: list[tuple[str, list[str]]] = []
        original = dealer.all_tags

        def observe(tenant_id: str, kb_ids: list[str], S: int = 1000) -> list[tuple[str, int]]:
            calls.append((tenant_id, kb_ids))
            return original(tenant_id, kb_ids, S)

        monkeypatch.setattr(dealer, "all_tags", observe)
        before = snapshot()
        result = _run_web(env, "tags")
        assert result["passed"] and len(result["responses"]) == 3
        assert calls and all(tenant == owners[0] and set(kbs).issubset(env["datasets"][:2]) for tenant, kbs in calls)
        after = snapshot()
        assert before == after
        record.update(result=result, source_unchanged=True, retriever_calls=calls)
        record_property("web_tags_readback", json.dumps(record))
