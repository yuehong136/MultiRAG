"""Realtime dictionary readback through the real fulltext and Memory queryers.

Uses an isolated Redis database; never writes the configured application's key.
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from redis import Redis

from core.nlp import synonym
from core.nlp.query import FulltextQueryer
from core.utils.redis_conn import REDIS_CONN
from memory.services.query import MsgTextQuery
from tests.support.services import container_host, service_images


@pytest.fixture(scope="module")
def isolated_redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    # A caller can supply an explicitly disposable endpoint (as this batch's
    # validation does). Routine CI gets its own container, never a business DB.
    if url := os.environ.get("SYNONYM_TEST_REDIS_URL"):
        yield url
        return
    from docker.errors import NotFound
    from testcontainers.redis import RedisContainer

    name = f"multirag-synonyms-{uuid4().hex[:12]}"
    directory: Path = tmp_path_factory.mktemp("synonym-redis")
    data = directory / "data"
    data.mkdir()
    manifest = directory / "resources.json"
    state = {"container": name, "dataDirectory": str(data), "keys": ["kevin_synonyms"], "db": 15, "cleanup": False}
    manifest.write_text(json.dumps(state))
    with ExitStack() as cleanup:
        container = RedisContainer(service_images()["redis"]).with_name(name).with_volume_mapping(str(data), "/data", mode="rw")
        cleanup.callback(container.stop)
        container.start()
        wrapped = container.get_wrapped_container()
        host, port = container_host(container), container.get_exposed_port(6379)
        state.update(containerId=wrapped.id, host=host, port=port)
        manifest.write_text(json.dumps(state))
        yield f"redis://{host}:{port}/15"
    with pytest.raises(NotFound):
        wrapped.reload()
    state["cleanup"] = True
    manifest.write_text(json.dumps(state))


@pytest.fixture
def synonym_redis(monkeypatch: pytest.MonkeyPatch, isolated_redis_url: str) -> Iterator[Redis]:
    with Redis.from_url(isolated_redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1) as client:
        assert client.ping()
        assert client.dbsize() == 0, "synonym integration requires an empty isolated Redis DB 15"
        monkeypatch.setattr(REDIS_CONN, "REDIS", client)
        try:
            yield client
        finally:
            client.delete("kevin_synonyms")
            # Independent connection readback proves exact key cleanup.
            with Redis.from_url(isolated_redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1) as observer:
                assert observer.get("kevin_synonyms") is None
                assert observer.dbsize() == 0
            optional = synonym._bounded_redis(client)
            optional.close()
            synonym._bounded_redis.cache_clear()


@pytest.mark.parametrize("queryer", [FulltextQueryer, MsgTextQuery])
def test_real_redis_dictionary_reaches_lexical_query_consumers(synonym_redis: Redis, queryer: type) -> None:
    payload = json.dumps({"项目术语": ["实时别名"]}, ensure_ascii=False)
    assert synonym_redis.set("kevin_synonyms", payload)
    assert synonym_redis.get("kevin_synonyms") == payload
    query = queryer()
    expression, keywords = query.question("项目术语")
    assert expression is not None
    assert "实时别名" in keywords
    assert "实时" in expression.matching_text


def test_bad_redis_refresh_keeps_a_good_snapshot(synonym_redis: Redis) -> None:
    assert synonym_redis.set("kevin_synonyms", json.dumps({"项目术语": ["有效别名"]}))
    dealer = FulltextQueryer().syn
    assert dealer.lookup("项目术语") == ["有效别名"]
    assert synonym_redis.set("kevin_synonyms", '["invalid dictionary"]')
    dealer.load_tm -= 3601
    dealer.lookup_num = 100
    assert dealer.lookup("项目术语") == ["有效别名"]
    assert synonym_redis.get("kevin_synonyms") == '["invalid dictionary"]'


def test_slow_redis_is_bounded_and_keeps_lexical_retrieval_available(synonym_redis: Redis) -> None:
    # Only the dedicated disposable server is paused. A configured application
    # server is never passed to this fixture.
    synonym_redis.client_pause(2500, all=True)
    query = FulltextQueryer()
    query.syn.dictionary = {"项目术语": ["本地别名"]}
    started = time.monotonic()
    try:
        expression, keywords = query.question("项目术语")
        assert expression is not None and "本地别名" in keywords
        assert time.monotonic() - started < 2
    finally:
        # Let the server resume before independent readback and key cleanup.
        time.sleep(2.6)
