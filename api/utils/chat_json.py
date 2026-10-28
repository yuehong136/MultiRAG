"""Normalize retrieval numerics only at the chat response boundary."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def sanitize_chat_json(value: Any) -> Any:
    """Copy JSON containers, replacing non-finite scores without mutating history.

    NumPy real scalars also need conversion: json.dumps cannot encode float32
    or int64 even when finite. Unsupported objects still fail serialization.
    """
    if isinstance(value, dict):
        return {key: sanitize_chat_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_chat_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_chat_json(item) for item in value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value
