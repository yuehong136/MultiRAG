"""Durable successful LLM substeps, scoped to exact graph/config/model inputs.

Missing/expired or malformed entries are cache misses. Redis failures propagate:
continuing as if the cache were empty would conceal loss of resume guarantees.
Only a successful checked stage publication may discard its substeps.
"""

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import networkx as nx

from core.graphrag.persistence import GraphRAGLease, current_writer, settled_io
from core.utils.redis_conn import REDIS_CONN

CHECKPOINT_TTL_SECONDS = 604800
RESOLUTION_CHECKPOINT = "resolution"
COMMUNITY_CHECKPOINT = "community"


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def model_identity(model: Any) -> dict[str, Any]:
    # Project effective model/transport inputs, excluding dates, status and usage
    # counters from TenantLLM.to_dict(). Accounting must not invalidate resume.
    # Credentials/options are included in the digest but never persisted raw.
    config = getattr(model, "model_config", None)
    fields = (
        "id",
        "tenant_id",
        "llm_factory",
        "llm_name",
        "mdl_type",
        "model_type",
        "model_types",
        "max_tokens",
        "is_tools",
        "api_base",
        "api_key",
        "api_key_payload",
        "instance_id",
        "provider_instance_id",
        "reference",
        "region",
    )
    effective = {key: config[key] for key in fields if key in config} if isinstance(config, dict) else None
    return {
        "class": f"{type(model).__module__}.{type(model).__qualname__}",
        "config": effective,
        "name": getattr(model, "llm_name", None),
        "max_length": getattr(model, "max_length", None),
    }


def stage_revision(graph: nx.Graph, config: dict[str, Any], model: Any) -> str:
    # NetworkX preserves insertion order. Sort to make a store roundtrip stable.
    data = nx.node_link_data(graph, edges="edges")
    data["nodes"] = sorted(data["nodes"], key=lambda node: node["id"])
    edges = []
    for edge in data["edges"]:
        edge = dict(edge)
        if not graph.is_directed() and edge["source"] > edge["target"]:
            edge["source"], edge["target"] = edge["target"], edge["source"]
        edges.append(edge)
    data["edges"] = sorted(edges, key=lambda edge: (edge["source"], edge["target"]))
    return fingerprint({"version": 1, "graph": data, "config": config, "model": model_identity(model)})


def resolution_checkpoint_key(entity_type: str, pairs: list[tuple[str, str]]) -> str:
    return fingerprint([entity_type, pairs])


def community_checkpoint_key(level: str, community_id: str, nodes: list[str]) -> str:
    return fingerprint([level, community_id, sorted(nodes)])


@dataclass(frozen=True)
class StageCheckpoints:
    tenant_id: str
    kb_id: str
    stage: str
    revision: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.kb_id or self.stage not in {RESOLUTION_CHECKPOINT, COMMUNITY_CHECKPOINT} or not re.fullmatch(r"[0-9a-f]{64}", self.revision):
            raise ValueError("Invalid GraphRAG checkpoint scope")

    @property
    def index_key(self) -> str:
        return f"graphrag:checkpoint:{fingerprint([self.tenant_id, self.kb_id])}:{self.stage}:{self.revision}:index"

    def data_key(self, key: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid GraphRAG checkpoint key")
        return self.index_key.removesuffix("index") + key

    def _pages(self, client: Any, page_size: int) -> Iterator[list[str]]:
        cursor = 0
        while True:
            cursor, members = client.sscan(self.index_key, cursor=cursor, count=page_size)
            page = []
            for member in members:
                if isinstance(member, bytes):
                    member = member.decode("ascii", errors="replace")
                if isinstance(member, str) and re.fullmatch(r"[0-9a-f]{64}", member):
                    page.append(member)
                else:
                    logging.warning("Ignoring invalid GraphRAG checkpoint index member")
            yield page
            if int(cursor) == 0:
                break

    def _load(self, page_size: int) -> dict[str, Any]:
        client = REDIS_CONN.REDIS
        result = {}
        for keys in self._pages(client, page_size):
            if not keys:
                continue
            values = client.mget([self.data_key(key) for key in keys])
            for key, value in zip(keys, values, strict=True):
                if value is None:
                    continue
                try:
                    envelope = json.loads(value)
                    if (
                        not isinstance(envelope, dict)
                        or envelope.get("revision") != self.revision
                        or envelope.get("scope") != self.index_key
                        or envelope.get("key") != key
                        or "payload" not in envelope
                    ):
                        raise ValueError("Foreign checkpoint envelope")
                    result[key] = envelope["payload"]
                except (ValueError, TypeError, UnicodeError):
                    logging.warning("Ignoring corrupt/foreign GraphRAG checkpoint")
        return result

    async def load(self, *, page_size: int = 128) -> dict[str, Any]:
        if page_size < 1:
            raise ValueError("Checkpoint page size must be positive")
        return await settled_io(self._load, page_size)

    def _save(self, key: str, payload: Any, lease: GraphRAGLease | None = None) -> None:
        envelope = {"revision": self.revision, "scope": self.index_key, "key": key, "payload": payload}
        encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if lease is not None:
            # Fence at Redis execution time, including after a stalled request.
            # Check the index type before any writes (Lua errors do not roll back).
            saved = REDIS_CONN.REDIS.eval(
                """
                if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
                local kind = redis.call('TYPE', KEYS[3]).ok
                if kind ~= 'none' and kind ~= 'set' then
                    return redis.error_reply('Invalid GraphRAG checkpoint index type')
                end
                redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
                redis.call('SADD', KEYS[3], ARGV[3])
                redis.call('EXPIRE', KEYS[3], ARGV[4])
                return 1
                """,
                3,
                lease.key,
                self.data_key(key),
                self.index_key,
                lease.lock.local.token,
                encoded,
                key,
                CHECKPOINT_TTL_SECONDS,
            )
            if not saved:
                raise RuntimeError("GraphRAG writer lease was lost during checkpoint publication")
            return
        with REDIS_CONN.REDIS.pipeline(transaction=True) as pipeline:
            pipeline.set(self.data_key(key), encoded, ex=CHECKPOINT_TTL_SECONDS)
            pipeline.sadd(self.index_key, key)
            pipeline.expire(self.index_key, CHECKPOINT_TTL_SECONDS)
            results = pipeline.execute()
        if len(results) != 3 or not results[0] or not results[2]:
            raise RuntimeError("GraphRAG checkpoint publication failed")

    async def save(self, key: str, payload: Any) -> None:
        await settled_io(self._save, key, payload, current_writer.get())

    def _cleanup(self, page_size: int, lease: GraphRAGLease | None = None) -> None:
        client = REDIS_CONN.REDIS

        def delete(keys: list[str]) -> None:
            if lease is None:
                client.delete(*keys)
                return
            deleted = client.eval(
                """
                if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
                for i = 2, #KEYS do redis.call('DEL', KEYS[i]) end
                return 1
                """,
                len(keys) + 1,
                lease.key,
                *keys,
                lease.lock.local.token,
            )
            if not deleted:
                raise RuntimeError("GraphRAG writer lease was lost during checkpoint cleanup")

        for keys in self._pages(client, page_size):
            if keys:
                delete([self.data_key(key) for key in keys])
        delete([self.index_key])

    async def cleanup(self, *, page_size: int = 128) -> None:
        if page_size < 1:
            raise ValueError("Checkpoint page size must be positive")
        await settled_io(self._cleanup, page_size, current_writer.get())
