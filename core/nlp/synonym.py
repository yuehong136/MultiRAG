import json
import logging
import os
import re
import threading
import time
from functools import lru_cache
from typing import Protocol, runtime_checkable

from nltk.corpus import wordnet
from redis import ConnectionPool, RedisError
from redis import Redis as RedisClient
from redis.backoff import NoBackoff
from redis.retry import Retry

from common.file_utils import get_project_base_directory

# Forces NLTK to load the corpus synchronously once, preventing concurrent tasks
# from triggering the lazy-loading race condition.
try:
    wordnet.ensure_loaded()
except Exception:
    logging.warning("Fail to load wordnet.ensure_loaded()")


@runtime_checkable
class RedisReader(Protocol):
    def get(self, key: str) -> str | bytes | None: ...


@lru_cache(maxsize=1)
def _bounded_redis(source: RedisClient) -> RedisClient:
    # Reuse the configured endpoint/authentication, but never inherit unbounded
    # timeouts or retries for this optional retrieval enhancement. The cached
    # client owns its pool; the application's shared pool remains untouched.
    options = dict(source.connection_pool.connection_kwargs)
    options.update(socket_connect_timeout=0.5, socket_timeout=0.5, retry=Retry(NoBackoff(), 0), retry_on_error=[], health_check_interval=0)
    pool = ConnectionPool(connection_class=source.connection_pool.connection_class, max_connections=4, **options)
    return RedisClient.from_pool(pool)


def redis_client() -> RedisClient | None:
    """Borrow Redis configuration without a health probe or any network IO."""
    from core.utils.redis_conn import REDIS_CONN

    source = REDIS_CONN.REDIS
    return _bounded_redis(source) if source is not None else None


def _dictionary(value: object) -> dict[str, str | list[str]]:
    if not isinstance(value, dict):
        raise ValueError("Synonyms must be a JSON object")
    result: dict[str, str | list[str]] = {}
    for key, terms in value.items():
        if not isinstance(key, str) or not (isinstance(terms, str) or (isinstance(terms, list) and all(isinstance(term, str) for term in terms))):
            raise ValueError("Synonyms must map strings to strings or string lists")
        result[re.sub(r"[ \t]+", " ", key.strip().lower())] = terms
    return result


class Dealer:
    def __init__(self, redis: RedisReader | None = None) -> None:

        self.lookup_num = 100000000
        self.load_tm = time.time() - 1000000
        self.dictionary: dict[str, str | list[str]] = {}
        self._load_lock = threading.Lock()
        path = os.path.join(get_project_base_directory(), "core/res", "synonym.json")
        try:
            with open(path, encoding="utf-8") as f:
                self.dictionary = _dictionary(json.load(f))
        except Exception:
            logging.warning("Missing synonym.json")
            self.dictionary = {}

        if not redis:
            logging.warning("Realtime synonym is disabled, since no redis connection.")
        if not len(self.dictionary.keys()):
            logging.warning("Fail to load synonym")

        self.redis = redis
        # Queryers may be constructed during application imports. Read Redis
        # only on first lookup so startup never depends on its availability.

    def load(self) -> None:
        if self.redis is None or not self._load_lock.acquire(blocking=False):
            return
        try:
            if self.lookup_num < 100 or time.time() - self.load_tm < 3600:
                return
            self.load_tm = time.time()
            self.lookup_num = 0
            try:
                data = self.redis.get("kevin_synonyms")
                if data:
                    self.dictionary = _dictionary(json.loads(data))
            except (RedisError, ValueError, TypeError):
                # Keep the last valid dictionary (or the bundled one). Do not
                # expose Redis credentials or dictionary content in logs.
                logging.warning("Realtime synonyms unavailable; retaining the current dictionary.")
        finally:
            self._load_lock.release()

    def lookup(self, tk: str, topn: int = 8) -> list[str]:
        if not tk or not isinstance(tk, str):
            return []

        # 1) Check the custom dictionary first (both keys and tk are already lowercase)
        self.lookup_num += 1
        self.load()
        key = re.sub(r"[ \t]+", " ", tk.strip().lower())
        res = self.dictionary.get(key, [])
        if isinstance(res, str):
            res = [res]
        if res:  # Found in dictionary → return directly
            return res[:topn]

        # 2) If not found and tk is purely alphabetical → fallback to WordNet
        if re.fullmatch(r"[a-z]+", tk):
            wn_set = {re.sub("_", " ", syn.name().split(".")[0]) for syn in wordnet.synsets(tk)}
            wn_set.discard(tk)  # Remove the original token itself
            wn_res = [t for t in wn_set if t]
            return wn_res[:topn]

        # 3) Nothing found in either source
        return []


if __name__ == "__main__":
    dl = Dealer()
    print(dl.dictionary)
