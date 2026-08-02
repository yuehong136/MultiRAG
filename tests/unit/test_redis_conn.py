from core.utils.redis_conn import REDIS_CONN


def test_zcard_returns_cardinality(monkeypatch):
    class FakeRedis:
        def zcard(self, key: str) -> int:
            assert key == "queue"
            return 3

    monkeypatch.setattr(REDIS_CONN, "REDIS", FakeRedis())

    assert REDIS_CONN.zcard("queue") == 3


def test_zcard_reconnects_and_returns_zero_on_error(monkeypatch):
    class FailingRedis:
        def zcard(self, key: str) -> int:
            raise RuntimeError(key)

    reopened: list[bool] = []
    monkeypatch.setattr(REDIS_CONN, "REDIS", FailingRedis())
    monkeypatch.setattr(REDIS_CONN, "__open__", lambda: reopened.append(True))

    assert REDIS_CONN.zcard("queue") == 0
    assert reopened == [True]
