"""Optional realtime synonyms must not compromise lexical retrieval."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis import Redis
from redis.exceptions import ConnectionError

from core.nlp import synonym
from core.nlp.query import FulltextQueryer
from memory.services.query import MsgTextQuery


def test_fresh_process_import_and_construction_perform_no_network(tmp_path: Path) -> None:
    overlay = tmp_path / "redis.json"
    overlay.write_text(json.dumps({"redis": {"host": "127.0.0.1:1", "db": 15, "password": "", "username": ""}}))
    overlay.chmod(0o600)
    script = """
import socket
attempts = []
def forbidden(self, address):
    attempts.append(address)
    raise AssertionError('query construction must not open network connections')
socket.socket.connect = forbidden
from core.nlp.query import FulltextQueryer
from memory.services.query import MsgTextQuery
FulltextQueryer()
MsgTextQuery()
assert not attempts, 'query import/construction attempted network IO'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, MULTIRAG_CONFIG_OVERLAY_FILE=str(overlay)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("queryer", [FulltextQueryer, MsgTextQuery])
def test_queryers_load_redis_only_on_lookup(monkeypatch: pytest.MonkeyPatch, queryer: type) -> None:
    reader = SimpleNamespace(get=Mock())
    reader.get.return_value = json.dumps({"项目术语": ["实时别名"]})
    monkeypatch.setattr(synonym, "redis_client", lambda: reader)
    query = queryer()
    reader.get.assert_not_called()
    assert query.syn.lookup("项目术语") == ["实时别名"]
    reader.get.assert_called_once_with("kevin_synonyms")
    for _ in range(200):
        assert query.syn.lookup("项目术语") == ["实时别名"]
    reader.get.assert_called_once()


@pytest.mark.parametrize("queryer", [FulltextQueryer, MsgTextQuery])
def test_queryers_work_without_redis(monkeypatch: pytest.MonkeyPatch, queryer: type) -> None:
    monkeypatch.setattr(synonym, "redis_client", lambda: None)
    query = queryer()
    query.syn.dictionary = {"项目术语": ["本地别名"]}
    expression, keywords = query.question("项目术语")
    assert expression is not None
    assert "本地别名" in keywords


@pytest.mark.parametrize("data", ["{", "[]", '"bad"', '{"项目术语": null}', '{"项目术语": [1]}'])
def test_bad_redis_payload_retains_last_good_dictionary(data: str) -> None:
    reader = SimpleNamespace(get=Mock())
    reader.get.return_value = data
    dealer = synonym.Dealer(reader)
    dealer.dictionary = {"项目术语": ["本地别名"]}
    assert dealer.lookup("项目术语") == ["本地别名"]
    reader.get.assert_called_once()


def test_redis_outage_retains_dictionary_and_does_not_retry_every_lookup(caplog: pytest.LogCaptureFixture) -> None:
    reader = SimpleNamespace(get=Mock())
    reader.get.side_effect = ConnectionError("test-only credential must not be logged")
    dealer = synonym.Dealer(reader)
    dealer.dictionary = {"项目术语": ["本地别名"]}
    for _ in range(200):
        assert dealer.lookup("项目术语") == ["本地别名"]
    reader.get.assert_called_once()
    assert "retaining the current dictionary" in caplog.text
    assert "credential must not be logged" not in caplog.text


def test_refresh_requires_both_one_hour_and_one_hundred_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10_000.0]
    monkeypatch.setattr(synonym.time, "time", lambda: now[0])
    reader = SimpleNamespace(get=Mock())
    reader.get.side_effect = [json.dumps({"项目术语": "首版"}), json.dumps({"项目术语": ["新版"]}), ""]
    dealer = synonym.Dealer(reader)
    assert dealer.lookup("项目术语") == ["首版"]
    now[0] += 3601
    for _ in range(99):
        assert dealer.lookup("项目术语") == ["首版"]
    assert dealer.lookup("项目术语") == ["新版"]
    now[0] += 3601
    for _ in range(100):
        assert dealer.lookup("项目术语") == ["新版"]
    assert reader.get.call_count == 3


def test_concurrent_refresh_does_not_block_cached_lookups() -> None:
    entered, release = Event(), Event()

    def read(key: str) -> str:
        assert key == "kevin_synonyms"
        entered.set()
        assert release.wait(timeout=3)
        return json.dumps({"项目术语": ["新版"]})

    reader = SimpleNamespace(get=Mock())
    reader.get.side_effect = read
    dealer = synonym.Dealer(reader)
    dealer.dictionary = {"项目术语": ["旧版"]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dealer.lookup, "项目术语")
        try:
            assert entered.wait(timeout=3)
            assert dealer.lookup("项目术语") == ["旧版"]
        finally:
            release.set()
        assert future.result(timeout=3) == ["新版"]
    reader.get.assert_called_once()


def test_case_normalization_topn_and_wordnet_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = SimpleNamespace(get=Mock())
    reader.get.return_value = json.dumps({" Project   Term ": ["one", "two"]})
    dealer = synonym.Dealer(reader)
    assert dealer.lookup("PROJECT\tTERM", topn=1) == ["one"]
    monkeypatch.setattr(synonym, "wordnet", SimpleNamespace(synsets=lambda term: [SimpleNamespace(name=lambda: "fallback.n.01")]))
    assert dealer.lookup("missing") == ["fallback"]


def test_optional_client_has_bounded_io_and_reuses_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.utils.redis_conn import REDIS_CONN

    source = Redis(host="127.0.0.1", port=1, db=13, decode_responses=True)
    monkeypatch.setattr(REDIS_CONN, "REDIS", source)
    client = synonym.redis_client()
    assert synonym.redis_client() is client
    options = client.connection_pool.connection_kwargs
    assert (options["host"], options["port"], options["db"]) == ("127.0.0.1", 1, 13)
    assert options["socket_connect_timeout"] == options["socket_timeout"] == 0.5
    assert options["retry"].get_retries() == 0
    assert source.connection_pool.connection_kwargs["socket_timeout"] is None
    monkeypatch.setattr(REDIS_CONN, "REDIS", None)
    assert synonym.redis_client() is None
    client.close()
    source.close()
    synonym._bounded_redis.cache_clear()
