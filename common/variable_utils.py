"""Variable path traversal shared by Agent execution and input preflight."""

import json
from typing import Any


def get_variable_param_value(obj: Any, path: str) -> Any:
    cur = obj
    if not path:
        return cur
    for key in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except (TypeError, ValueError):
                return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            cur = getattr(cur, key, None)
    return cur
