#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import ast
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

import json_repair

from common.metadata_config import canonical_field, field_schema

_OPERATOR_ALIASES = {"is": "=", "not is": "≠", ">=": "≥", "<=": "≤", "!=": "≠"}


def convert_conditions(metadata_condition: dict[str, Any] | None) -> list[dict[str, Any]]:
    if metadata_condition is None:
        metadata_condition = {}
    return [{"op": _OPERATOR_ALIASES.get(cond["comparison_operator"], cond["comparison_operator"]), "key": cond["name"], "value": cond["value"]} for cond in metadata_condition.get("conditions", [])]


def meta_filter(metas: dict[str, Any], filters: list[dict[str, Any]], logic: str = "and") -> list[str]:
    """Match metadata, ignoring string case in lists only for in/not in."""
    doc_ids: set[str] | None = None

    def filter_out(v2docs: dict[Any, list[str]], operator: str, value: Any) -> list[str]:
        operator = _OPERATOR_ALIASES.get(operator, operator)
        ids: list[str] = []
        for input, docids in v2docs.items():
            if operator in ["=", "≠", ">", "<", "≥", "≤"]:
                # Check if input is in YYYY-MM-DD date format
                input_str = str(input).strip()
                value_str = str(value).strip()

                # Strict date format detection: YYYY-MM-DD (must be 10 chars with correct format)
                is_input_date = len(input_str) == 10 and input_str[4] == "-" and input_str[7] == "-" and input_str[:4].isdigit() and input_str[5:7].isdigit() and input_str[8:10].isdigit()

                is_value_date = len(value_str) == 10 and value_str[4] == "-" and value_str[7] == "-" and value_str[:4].isdigit() and value_str[5:7].isdigit() and value_str[8:10].isdigit()

                if is_value_date:
                    # Query value is in date format
                    if is_input_date:
                        # Data is also in date format: perform date comparison
                        input = input_str
                        value = value_str
                    else:
                        # Data is not in date format: skip this record (no match)
                        continue
                else:
                    # Query value is not in date format: use original logic
                    try:
                        if isinstance(input, list):
                            input = input[0]
                        input = ast.literal_eval(input)
                        value = ast.literal_eval(value)
                    except Exception:
                        pass

                    # Convert strings to lowercase
                    if isinstance(input, str):
                        input = input.lower()
                    if isinstance(value, str):
                        value = value.lower()
            else:
                # Non-comparison operators: maintain original logic
                if isinstance(input, str):
                    input = input.lower()
                elif operator in ("in", "not in") and isinstance(input, list):
                    input = [item.lower() if isinstance(item, str) else item for item in input]
                if isinstance(value, str):
                    value = value.lower()
                elif operator in ("in", "not in") and isinstance(value, list):
                    value = [item.lower() if isinstance(item, str) else item for item in value]

            matched = False
            try:
                if operator == "contains":
                    matched = str(input).find(value) >= 0 if not isinstance(input, list) else any(str(i).find(value) >= 0 for i in input)
                elif operator == "not contains":
                    matched = str(input).find(value) == -1 if not isinstance(input, list) else all(str(i).find(value) == -1 for i in input)
                elif operator == "in":
                    matched = input in value if not isinstance(input, list) else all(i in value for i in input)
                elif operator == "not in":
                    matched = input not in value if not isinstance(input, list) else all(i not in value for i in input)
                elif operator == "start with":
                    matched = str(input).lower().startswith(str(value).lower()) if not isinstance(input, list) else "".join([str(i).lower() for i in input]).startswith(str(value).lower())
                elif operator == "end with":
                    matched = str(input).lower().endswith(str(value).lower()) if not isinstance(input, list) else "".join([str(i).lower() for i in input]).endswith(str(value).lower())
                elif operator == "empty":
                    matched = not input
                elif operator == "not empty":
                    matched = bool(input)
                elif operator == "=":
                    matched = input == value
                elif operator == "≠":
                    matched = input != value
                elif operator == ">":
                    matched = input > value
                elif operator == "<":
                    matched = input < value
                elif operator == "≥":
                    matched = input >= value
                elif operator == "≤":
                    matched = input <= value
            except Exception:
                pass

            if matched:
                ids.extend(docids)
        return ids

    for f in filters:
        k = f["key"]
        if k not in metas:
            # Key not found in metas: treat as no match
            ids = []
        else:
            v2docs = metas[k]
            ids = filter_out(v2docs, f["op"], f["value"])

        if doc_ids is None:
            doc_ids = set(ids)
        else:
            if logic == "and":
                doc_ids = doc_ids & set(ids)
                if not doc_ids:
                    return []
            else:
                doc_ids = doc_ids | set(ids)
    return list(doc_ids or [])


def _semi_auto_candidates(meta_data_filter: dict[str, Any], metas: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]] | None:
    """Validate the complete selected field set against available document values."""
    selection = meta_data_filter.get("semi_auto")
    if not isinstance(selection, list) or not selection:
        return None
    selected = {}
    constraints = {}
    for item in selection:
        key = item if isinstance(item, str) else item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key.strip():
            return None
        values = metas.get(key)
        if not isinstance(values, dict) or not any(isinstance(documents, list) and documents for documents in values.values()):
            return None
        selected[key] = values
        if isinstance(item, dict) and item.get("op"):
            if not isinstance(item["op"], str):
                return None
            constraints[key] = item["op"]
    return selected, constraints


async def apply_meta_data_filter(
    meta_data_filter: dict[str, Any] | None,
    metas: dict[str, Any],
    question: str,
    chat_mdl: Any = None,
    base_doc_ids: list[str] | None = None,
    manual_value_resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    metadata_refresher: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> list[str] | None:
    """
    Intersect metadata matches with a nonempty base document selection.

    meta_data_filter supports three modes:
    - auto: generate filter conditions via LLM (gen_meta_filter)
    - semi_auto: generate conditions using selected metadata keys only
    - manual: directly filter based on provided conditions

    Returns:
        Matching doc_ids, or ["-999"] when conditions yield no matches. An empty
        base selection means unrestricted retrieval, as it does without filters.
        Explicit no-filter and manual empty conditions preserve the selection;
        auto empty conditions retain the existing fallback. Semi-auto requires
        usable selected fields and generated predicates; stale/empty selections
        return the no-match sentinel. A refresher rechecks metadata after LLM IO.
    """
    from core.prompts.generator import gen_meta_filter  # move from the top of the file to avoid circular import

    doc_ids = list(base_doc_ids) if base_doc_ids else []

    if not meta_data_filter:
        return doc_ids

    method = meta_data_filter.get("method")
    filter_metas = metas

    if method == "auto":
        filters = await gen_meta_filter(chat_mdl, metas, question)
    elif method == "semi_auto":
        candidates = _semi_auto_candidates(meta_data_filter, metas)
        if candidates is None:
            return ["-999"]
        filter_metas, constraints = candidates
        filters = await gen_meta_filter(chat_mdl, filter_metas, question, constraints=constraints)
        # The selected directory/values may change while model generation awaits.
        # Use fresh values for matching, never drop a now-missing selected key.
        if metadata_refresher is not None:
            candidates = _semi_auto_candidates(meta_data_filter, await metadata_refresher())
            if candidates is None:
                return ["-999"]
            filter_metas, _ = candidates
        if not filters["conditions"] or any(condition.get("key") not in filter_metas for condition in filters["conditions"]):
            return ["-999"]
    elif method == "manual":
        manual_filters = meta_data_filter.get("manual", [])
        if manual_value_resolver:
            manual_filters = [manual_value_resolver(deepcopy(flt)) for flt in manual_filters]
        filters = {"conditions": manual_filters, "logic": meta_data_filter.get("logic", "and")}
    else:
        return doc_ids

    if not filters["conditions"]:
        return (doc_ids or None) if method in ("auto", "semi_auto") else doc_ids
    matches = meta_filter(filter_metas, filters["conditions"], filters.get("logic", "and"))
    if doc_ids:
        matched_ids = set(matches)
        matches = list(dict.fromkeys(doc_id for doc_id in doc_ids if doc_id in matched_ids))
    # Empty lists/None remove the document predicate in retrieval backends.
    return matches or ["-999"]


def dedupe_list(values: list) -> list:
    seen = set()
    deduped = []
    for item in values:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def update_metadata_to(metadata: dict[str, Any], meta: Any) -> dict[str, Any]:
    """Merge extracted values without dropping scalar or structured metadata."""
    if not meta:
        return metadata
    if isinstance(meta, str):
        try:
            meta = json_repair.loads(meta)
        except Exception:
            logging.error("Meta data format error.")
            return metadata
    if not isinstance(meta, dict):
        return metadata

    for k, v in meta.items():
        if isinstance(v, list) and v and all(isinstance(item, str) for item in v):
            v = dedupe_list(v)
        elif isinstance(v, (dict, list)):
            # Structured values are atomic; never concatenate them with tags.
            if k not in metadata:
                metadata[k] = deepcopy(v)
            continue
        elif not isinstance(v, (str, bool, int, float)) and v is not None:
            continue
        if k not in metadata:
            metadata[k] = deepcopy(v)
            continue
        if isinstance(metadata[k], list) and isinstance(v, (list, str)):
            if not all(isinstance(item, str) for item in metadata[k]):
                continue
            if isinstance(v, list):
                metadata[k] = metadata[k] + v
            else:
                metadata[k] = metadata[k] + [v]
            metadata[k] = dedupe_list(metadata[k])
        else:
            metadata[k] = v

    return metadata


def metadata_schema(metadata: dict | list | None) -> dict[str, Any]:
    if not metadata:
        return {}
    properties = {}

    for item in metadata:
        key = item.get("key") or item.get("name")
        if not key:
            continue

        properties[key] = field_schema(item)

    json_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }

    json_schema["additionalProperties"] = False
    return json_schema


def _is_json_schema(obj: dict) -> bool:
    if not isinstance(obj, dict):
        return False
    if "$schema" in obj:
        return True
    return obj.get("type") == "object" and isinstance(obj.get("properties"), dict)


def _is_metadata_list(obj: list[Any]) -> bool:
    if not isinstance(obj, list) or not obj:
        return False
    for item in obj:
        if not isinstance(item, dict):
            return False
        key = item.get("key") or item.get("name")
        if not isinstance(key, str) or not key:
            return False
        if item.get("enum") is not None and not isinstance(item["enum"], list):
            return False
        if item.get("description") is not None and not isinstance(item["description"], str):
            return False
        if item.get("descriptions") is not None and not isinstance(item["descriptions"], str):
            return False
    return True


def turn2jsonschema(obj: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if isinstance(obj, dict) and _is_json_schema(obj):
        return obj
    if isinstance(obj, list) and _is_metadata_list(obj):
        return metadata_schema([canonical_field(item) for item in obj])
    return {}


def build_metadata_config(parser_config: dict[str, Any]) -> dict[str, Any] | list[Any]:
    """Combine schema or legacy fields with built-ins, preserving schema constraints."""
    metadata_conf = parser_config.get("metadata", [])
    built_in_metadata = parser_config.get("built_in_metadata") or []
    built_in_metadata = deepcopy(built_in_metadata) if isinstance(built_in_metadata, list) else []
    if isinstance(metadata_conf, dict):
        if not isinstance(metadata_conf.get("properties"), dict):
            metadata_conf = {"type": "object", "properties": {}}
        else:
            metadata_conf = deepcopy(metadata_conf)
        if built_in_metadata:
            metadata_conf["properties"].update(turn2jsonschema(built_in_metadata).get("properties", {}))
        return metadata_conf
    if isinstance(metadata_conf, list):
        return deepcopy(metadata_conf) + built_in_metadata
    return built_in_metadata
