"""Trusted, expiring ownership of one execution attempt, shared with Go."""

import json
from typing import Literal, TypedDict, cast

from redis import Redis

from common.exceptions import TaskCanceledException
from core.utils.redis_conn import REDIS_CONN

TASK_RUNTIME_TTL = 24 * 60 * 60
TASK_CANCEL_MARKER = "[cancel_requested]"
TASK_RUNTIME_PREFIX = "task-runtime:v1:"
RuntimeFinish = Literal["unbound", "finished", "cancel_requested"]


class TaskBinding(TypedDict):
    version: int
    principal_id: str
    tenant_id: str
    resource_id: str
    kind: str
    state: str


# Compare the entire server-written binding. A concurrent finish or expiry must
# win over a stale request; keys and ownership never come from a request body.
CANCEL_RUNTIME_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local binding = cjson.decode(ARGV[1])
if binding.state ~= 'active' then return 0 end
binding.state = 'cancel_requested'
binding.cancel_token = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(binding), 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

RESTORE_RUNTIME_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local binding = cjson.decode(raw)
if binding.cancel_token ~= ARGV[2] then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('DEL', KEYS[2])
return 1
"""

FINISH_RUNTIME_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local binding = cjson.decode(raw)
if binding.version ~= 1 then return redis.error_reply('Invalid task runtime binding') end
if binding.state == 'cancel_requested' then return 2 end
if binding.state == 'finished' then return 1 end
if binding.state ~= 'active' then return redis.error_reply('Invalid task runtime state') end
if binding.state == 'active' then
    binding.state = 'finished'
    redis.call('SET', KEYS[1], cjson.encode(binding), 'EX', ARGV[1])
end
return 1
"""

DELETE_CANCEL_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


def runtime_redis() -> Redis:
    client = REDIS_CONN.REDIS
    if client is None:
        raise ConnectionError("Task runtime Redis is unavailable.")
    return cast(Redis, client)


def binding_key(task_id: str) -> str:
    return TASK_RUNTIME_PREFIX + task_id


def read_binding(task_id: str) -> tuple[str, TaskBinding] | None:
    raw = runtime_redis().get(binding_key(task_id))
    if raw is None:
        return None
    raw = raw.decode() if isinstance(raw, bytes) else str(raw)
    binding = json.loads(raw)
    if (
        not isinstance(binding, dict)
        or type(binding.get("version")) is not int
        or binding["version"] != 1
        or binding.get("kind") not in {"agent", "dataflow"}
        or binding.get("state") not in {"active", "cancel_requested", "finished"}
        or any(not isinstance(binding.get(key), str) or not binding[key] for key in ("principal_id", "tenant_id", "resource_id"))
    ):
        raise ValueError("Invalid task runtime binding.")
    return raw, cast(TaskBinding, binding)


def register_runtime(task_id: str, principal_id: str, tenant_id: str, resource_id: str, kind: Literal["agent", "dataflow"]) -> None:
    binding: TaskBinding = {"version": 1, "principal_id": principal_id, "tenant_id": tenant_id, "resource_id": resource_id, "kind": kind, "state": "active"}
    if not runtime_redis().set(binding_key(task_id), json.dumps(binding), ex=TASK_RUNTIME_TTL, nx=True):
        raise RuntimeError("Failed to register task runtime ownership.")


def finish_runtime(task_id: str) -> RuntimeFinish:
    """Atomically resolve finish against cancel, without reviving a cancelled run.

    Ordinary background tasks have no runtime binding and return unbound.
    Agent success must require finished before persisting or emitting success.
    """
    result = runtime_redis().eval(FINISH_RUNTIME_SCRIPT, 1, binding_key(task_id), TASK_RUNTIME_TTL)
    outcomes: dict[int, RuntimeFinish] = {0: "unbound", 1: "finished", 2: "cancel_requested"}
    if type(result) is not int or result not in outcomes:
        raise ConnectionError("Failed to resolve task runtime completion.")
    return outcomes[result]


def require_runtime_finish(task_id: str) -> None:
    outcome = finish_runtime(task_id)
    if outcome == "cancel_requested":
        raise TaskCanceledException(f"Task {task_id} has been canceled before completion.")
    if outcome == "unbound":
        raise RuntimeError("Task runtime ownership is unavailable at completion.")


def request_runtime_cancel(task_id: str, expected: str, token: str) -> bool:
    return bool(runtime_redis().eval(CANCEL_RUNTIME_SCRIPT, 2, binding_key(task_id), f"{task_id}-cancel", expected, token, TASK_RUNTIME_TTL))


def restore_runtime(task_id: str, expected: str, token: str) -> bool:
    return bool(runtime_redis().eval(RESTORE_RUNTIME_SCRIPT, 2, binding_key(task_id), f"{task_id}-cancel", expected, token, TASK_RUNTIME_TTL))
