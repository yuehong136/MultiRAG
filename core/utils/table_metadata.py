"""Table role selection and lossless, backend-independent metadata carriers."""

import copy
import json
from typing import Any

TABLE_METADATA = "__table_metadata__"
TABLE_ROW_KEY = "__table_row_key__"


def merge_table_parser_config_from_kb(task: dict[str, Any]) -> dict[str, Any]:
    """Keep current dataset defaults and then apply explicit document overrides.

    Uploads persist table_column_overrides so a later dataset settings change
    cannot silently reclassify that file. Older documents without overrides
    continue following dataset roles. The private marker is consumed here.
    """
    config = dict(task.get("parser_config") or {})
    if task.get("parser_id", "").lower() == "table":
        for key in ("table_column_mode", "table_column_roles", "table_column_names"):
            if key in (task.get("kb_parser_config") or {}):
                config[key] = task["kb_parser_config"][key]
        overrides = config.pop("table_column_overrides", {})
        config.update(overrides)
    return config


def table_column_roles(config: dict[str, Any]) -> dict[str, str]:
    if config.get("table_column_mode") != "manual":
        return {}
    roles = config.get("table_column_roles") or {}
    normalized = {name: "indexing" if role == "vectorize" else role for name, role in roles.items()}
    if any(role not in {"indexing", "metadata", "both"} for role in normalized.values()):
        raise ValueError("Invalid table column role.")
    return normalized


def value_key(value: Any) -> str:
    """Keep booleans distinct from numbers, and serialize only finite JSON values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def append_unique(target: list[Any], values: list[Any]) -> None:
    seen = {value_key(value) for value in target}
    for value in values:
        key = value_key(value)
        if key not in seen:
            target.append(value)
            seen.add(key)


def collect_table_metadata(chunks: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Consume raw carriers before storage; sparse rows never require field-name probing."""
    result: dict[str, list[Any]] = {}
    seen: dict[str, set[str]] = {}
    for chunk in chunks:
        for name, value in chunk.pop(TABLE_METADATA, {}).items():
            key = value_key(value)
            if key not in seen.setdefault(name, set()):
                result.setdefault(name, []).append(value)
                seen[name].add(key)
    return result


def replace_owned_metadata(existing: dict[str, Any], old_owned: dict[str, list[Any]], contributions: list[dict[str, list[Any]]]) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Remove only values previously added by the table parser, preserving user fields."""
    merged = copy.deepcopy(existing)
    for name, values in old_owned.items():
        if isinstance(merged.get(name), list):
            owned = {value_key(value) for value in values}
            remaining = [value for value in merged[name] if value_key(value) not in owned]
            if remaining:
                merged[name] = remaining
            else:
                merged.pop(name)
    owned_now: dict[str, list[Any]] = {}
    for contribution in contributions:
        for name, values in contribution.items():
            base = merged.get(name, [])
            base = list(base) if isinstance(base, list) else [base]
            base_seen = {value_key(item) for item in base}
            added = [value for value in values if value_key(value) not in base_seen]
            append_unique(base, added)
            merged[name] = base
            append_unique(owned_now.setdefault(name, []), added)
    return merged, {name: values for name, values in owned_now.items() if values}
