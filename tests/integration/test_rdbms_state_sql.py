"""RDBMS worker state commits and API protection in an isolated SQL database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service
from api.db.db_models import Connector, Connector2Kb, SyncLogs
from api.db.services.connector_service import ConnectorService, SyncLogsService
from common.constants import TaskStatus
from common.data_source.rdbms_connector import RDBMSConnector, rdbms_scope_signature


@pytest.mark.parametrize("source", ["postgresql", "mssql"])
def test_cursor_completion_rollback_parallel_kbs_and_config_save(bootstrapped_engine: sa.Engine, source: str) -> None:
    engine = bootstrapped_engine
    identifier = uuid4().hex
    conf = {"database": "controlled-source", "credentials": {"username": "reader"}, "timestamp_column": "seq"}
    signature = rdbms_scope_signature(conf, source)
    with Session(engine) as db:
        db.add(Connector(id=identifier, tenant_id=uuid4().hex, name="cursor state", source=source, input_type="poll", config=conf))
        for kb_id in ("a", "b"):
            db.add(Connector2Kb(id=uuid4().hex, connector_id=identifier, kb_id=kb_id))
        db.commit()

    def complete(kb: str, value: Decimal, fail: bool = False) -> None:
        with Session(engine) as db:
            task_id = uuid4().hex
            db.add(SyncLogs(id=task_id, connector_id=identifier, kb_id=kb, status=TaskStatus.RUNNING))
            db.commit()
            state = {"source": source, "scope": signature, "cursor": RDBMSConnector.serialize_cursor_value(value)}
            if fail:
                # Row changes are staged, then rolled back like failed completion.
                ConnectorService.stage_rdbms_sync_state(db, identifier, kb, state)
                db.rollback()
            else:
                SyncLogsService.complete_and_schedule_next(db, task_id, identifier, kb, datetime.now(UTC), rdbms_state=state)

    try:
        with ThreadPoolExecutor(2) as threads:
            list(threads.map(lambda pair: complete(*pair), [("a", Decimal("1.000001")), ("b", Decimal("2.000002"))]))
        complete("a", Decimal(999), fail=True)
        with Session(engine) as db:
            saved = db.get(Connector, identifier).config["rdbms_sync_scopes"]
            assert {kb: RDBMSConnector.deserialize_cursor_value(item["cursor"]) for kb, item in saved.items()} == {"a": Decimal("1.000001"), "b": Decimal("2.000002")}
            assert connector_api_service.update_connector(db, identifier, {"config": conf | {"custom": "keep", "rdbms_sync_scopes": {}}})[0]
        with Session(engine) as db:
            config = db.get(Connector, identifier).config
            assert config["rdbms_sync_scopes"] == saved and config["custom"] == "keep"
            with pytest.raises(RuntimeError, match="changed"):
                ConnectorService.stage_rdbms_sync_state(db, identifier, "a", {"source": source, "scope": "stale", "cursor": 500})
    finally:
        with Session(engine) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == identifier))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id == identifier))
            db.execute(sa.delete(Connector).where(Connector.id == identifier))
            db.commit()
