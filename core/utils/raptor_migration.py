"""Confirm complete summary replacement before removing a previous tree."""

import json
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from common.doc_store.document_history import confirm_history_visibility, delete_document_history, document_history, restore_document_history
from common.exceptions import TaskCanceledException
from core.utils.raptor_utils import collect_raptor_chunk_ids


def summary_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    fields = {str(row.get("id", row.get("pk"))): row for row in rows}
    return {key: fields[key] for key in collect_raptor_chunk_ids(fields)}


def replace_raptor_summaries(
    store: Any,
    index: str,
    dataset: str,
    chunks: list[dict[str, Any]],
    previous: dict[str, dict[str, dict[str, Any]]],
    canceled: Callable[[], bool],
    batch_size: int,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Run under the dataset writer lock; compensate only this attempt's IDs."""
    if canceled():
        raise TaskCanceledException("RAPTOR replacement canceled")
    originals = {doc: document_history(store, index, dataset, doc) for doc in previous}
    for doc, expected in previous.items():
        if summary_rows(originals[doc]) != expected:
            raise RuntimeError("RAPTOR summaries changed during generation; retry the task")
    inserted: dict[str, list[str]] = {}
    for chunk in chunks:
        doc = chunk["doc_id"]
        if doc not in originals:
            raise ValueError("RAPTOR output document has no migration snapshot")
        identifier = str(chunk.get("id", chunk.get("pk")))
        # A task-specific ID prevents partial replacement overwriting old content.
        if any(str(row.get("id", row.get("pk"))) == identifier for row in originals[doc]):
            raise RuntimeError("RAPTOR generation ID already exists")
        inserted.setdefault(doc, []).append(identifier)
    written: dict[str, dict[str, dict[str, Any]]] = {doc: {} for doc in previous}
    mutated = False
    try:
        for start in range(0, len(chunks), batch_size):
            if canceled():
                raise TaskCanceledException("RAPTOR replacement canceled")
            mutated = True
            receipt = store.insert(chunks[start : start + batch_size], index, dataset)
            if not isinstance(receipt, list) or receipt:
                raise RuntimeError("RAPTOR insertion was not acknowledged")
        confirm_history_visibility(store, index)
        for doc, identifiers in inserted.items():
            actual = {str(row.get("id", row.get("pk"))): row for row in document_history(store, index, dataset, doc)}
            expected = {str(row.get("id", row.get("pk"))): row for row in chunks if row["doc_id"] == doc}
            written[doc] = {identifier: actual[identifier] for identifier in identifiers if identifier in actual}
            for identifier in identifiers:
                stored = actual.get(identifier, {})
                backend = store.db_type()
                if backend == "infinity" and stored:
                    # Keep native rows for recovery; compare logical aliases through
                    # the connector's read conversion instead of raw column names.
                    stored = store.get_fields(pd.DataFrame([stored]), list(expected[identifier]))[identifier]
                for key, value in expected[identifier].items():
                    if key in {"id", "pk"}:
                        continue
                    if key not in stored:
                        raise RuntimeError("RAPTOR inserted field could not be confirmed")
                    actual_value = stored[key]
                    if key == "kb_id":
                        value = value[0] if isinstance(value, list) and len(value) == 1 else value
                        actual_value = actual_value[0] if isinstance(actual_value, list) and len(actual_value) == 1 else actual_value
                    if backend == "infinity":
                        if key == "extra" and isinstance(value, dict):
                            actual_value = json.loads(actual_value)
                        elif store.field_keyword(key) and isinstance(value, str):
                            value = [item for item in value.split("###") if item]
                    if backend in {"milvus", "infinity"} and (key == "vector" or key.endswith("_vec")):
                        # Native float vectors store IEEE float32, while model
                        # adapters may return Python doubles. Compare that exact
                        # representation, including shape and finite values.
                        expected_vector = np.asarray(value, dtype=np.float32)
                        actual_vector = np.asarray(actual_value, dtype=np.float32)
                        if not np.isfinite(expected_vector).all() or not np.array_equal(actual_vector, expected_vector):
                            raise RuntimeError("RAPTOR inserted vector could not be confirmed")
                    elif backend == "infinity" and isinstance(value, float):
                        # Infinity's mapping uses float32 scalar columns too.
                        if not np.isfinite(value) or np.float32(actual_value) != np.float32(value):
                            raise RuntimeError("RAPTOR inserted content could not be confirmed")
                    elif actual_value != value:
                        raise RuntimeError("RAPTOR inserted content could not be confirmed")
        if canceled():
            raise TaskCanceledException("RAPTOR replacement canceled")
        for doc, old in previous.items():
            if canceled():
                raise TaskCanceledException("RAPTOR replacement canceled")
            if old:
                mutated = True
                delete_document_history(store, index, dataset, doc, ids=list(old))
        if canceled():
            raise TaskCanceledException("RAPTOR replacement canceled")
        for doc, expected in written.items():
            if summary_rows(document_history(store, index, dataset, doc)) != expected:
                raise RuntimeError("RAPTOR confirmed tree changed before completion")
        return written
    except BaseException:
        if mutated:
            # Restore the complete old payload/vector for precisely affected IDs.
            # No document-wide deletion: normal chunks and other KBs are untouched.
            for doc, rows in originals.items():
                identifiers = list(dict.fromkeys([*previous[doc], *inserted.get(doc, [])]))
                restore_document_history(store, index, dataset, doc, rows, ids=identifiers)
        raise
