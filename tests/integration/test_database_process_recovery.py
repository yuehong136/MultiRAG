"""A killed process preserves committed service writes and rolls back open work."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import User
from api.db.services.user_service import UserService

_CHILD = """
import os
import sys
import sqlalchemy as sa
from sqlalchemy.orm import Session
from api.db.db_models import User
from api.db.services.user_service import UserService
url = sa.make_url(os.environ['MULTIRAG_PROCESS_TEST_DSN'])
assert url.database.startswith('multirag_test_')
engine = sa.create_engine(url)
uid = os.environ['MULTIRAG_PROCESS_TEST_USER']
with Session(engine) as db:
    UserService.insert(db, id=uid, email=uid+'@process.test', nickname='committed', password='unused')
with Session(engine) as db:
    user = db.get(User, uid)
    user.nickname = 'uncommitted'
    db.flush()
    print('transaction-open', flush=True)
    sys.stdin.readline()
"""


def test_process_death_rolls_back_open_transaction_and_allows_recovery(bootstrapped_engine: sa.Engine) -> None:
    uid = uuid4().hex
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "MULTIRAG_PROCESS_TEST_DSN": bootstrapped_engine.url.render_as_string(hide_password=False), "MULTIRAG_PROCESS_TEST_USER": uid},
    )
    try:
        assert child.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=20).strip() == "transaction-open"
            finally:
                child.kill()
                child.wait(timeout=10)
        assert child.returncode != 0
        # A fresh connection observes the commit, never the killed transaction.
        with bootstrapped_engine.connect() as reader:
            assert reader.scalar(sa.select(User.nickname).where(User.id == uid)) == "committed"
        with Session(bootstrapped_engine) as db:
            assert UserService.update_by_id(db, uid, {"nickname": "recovered"}) == 1
        with bootstrapped_engine.connect() as reader:
            assert reader.scalar(sa.select(User.nickname).where(User.id == uid)) == "recovered"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        with bootstrapped_engine.begin() as connection:
            connection.execute(sa.delete(User).where(User.id == uid))
