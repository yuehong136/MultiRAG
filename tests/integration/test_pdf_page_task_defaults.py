"""Task page markers survive an actual scratch database write."""

from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from api.db.db_models import Task
from common.constants import MAXIMUM_TASK_PAGE_NUMBER


def test_non_page_task_marker_round_trips(bootstrapped_engine: Engine) -> None:
    task_id = uuid4().hex
    with Session(bootstrapped_engine) as db:
        db.add(Task(id=task_id, doc_id=uuid4().hex, task_type="dataflow"))
        db.flush()
        db.expire_all()

        saved = db.scalar(select(Task).where(Task.id == task_id))
        assert saved is not None
        assert saved.from_page == 0
        assert saved.to_page == MAXIMUM_TASK_PAGE_NUMBER
        db.rollback()
