"""Availability-only update scripts and complete bulk acknowledgements."""

from collections.abc import Mapping, Sequence
from typing import Any


def availability_parent_ids(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Resolve parents from complete, dataset-scoped rows, including old mothers."""
    ids: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    for row in rows:
        doc_id, chunk_id, mom_id = row.get("doc_id"), row.get("id"), row.get("mom_id")
        if not isinstance(doc_id, str) or not doc_id or not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError("Invalid availability relationship row")
        if chunk_id in ids.setdefault(doc_id, set()):
            raise ValueError("Duplicate availability relationship row")
        ids[doc_id].add(chunk_id)
        if mom_id is not None and not isinstance(mom_id, str):
            raise ValueError("Invalid availability parent reference")
        if mom_id:
            references.setdefault(doc_id, set()).add(mom_id)
    return {doc_id: sorted(chunk_ids & references.get(doc_id, set())) for doc_id, chunk_ids in ids.items()}


def availability_search_parents(client: Any, index_name: str, query: dict[str, Any]) -> dict[str, list[str]]:
    """Read every relationship from a scroll snapshot; reject partial reads."""
    rows: list[dict[str, Any]] = []
    scroll_id = None
    expected = None
    try:
        response = client.search(
            index=index_name,
            body={"query": query, "_source": ["id", "pk", "doc_id", "mom_id"], "sort": ["_doc"], "size": 1000, "track_total_hits": True},
            scroll="1m",
            allow_partial_search_results=False,
        )
        while True:
            raw = getattr(response, "body", response)
            if not isinstance(raw, Mapping):
                raise ValueError("Unknown availability relationship response")
            scroll_id = raw.get("_scroll_id") or scroll_id
            shards = raw.get("_shards", {})
            if raw.get("timed_out") is not False or shards.get("failed") != 0 or type(shards.get("total")) is not int or shards["total"] < 1 or shards.get("successful") != shards["total"]:
                raise ValueError("Incomplete availability relationship response")
            hits = raw.get("hits", {})
            total = hits.get("total")
            if isinstance(total, Mapping):
                total = total.get("value") if total.get("relation") == "eq" else None
            if type(total) is not int or total < 0 or (expected is not None and total != expected):
                raise ValueError("Unknown availability relationship count")
            expected = total
            page = hits.get("hits")
            if not isinstance(page, list):
                raise ValueError("Unknown availability relationship rows")
            for hit in page:
                source = hit.get("_source")
                if not isinstance(source, Mapping):
                    raise ValueError("Missing availability relationship source")
                rows.append({**source, "id": source.get("id", source.get("pk", hit.get("_id")))})
            if len(rows) > expected:
                raise ValueError("Inconsistent availability relationship count")
            if not page:
                if len(rows) != expected:
                    raise ValueError("Truncated availability relationships")
                return availability_parent_ids(rows)
            if not scroll_id:
                raise ValueError("Missing availability scroll cursor")
            response = client.scroll(scroll_id=scroll_id, scroll="1m")
    finally:
        if scroll_id:
            client.clear_scroll(scroll_id=scroll_id)


def availability_script(status: int, parents: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "source": """
def id = ctx._source.containsKey('id') ? ctx._source.id :
    (ctx._source.containsKey('pk') ? ctx._source.pk : ctx._id);
boolean parent = ctx._source.containsKey('mom_id') &&
    ctx._source.mom_id != null && ctx._source.mom_id != '' && ctx._source.mom_id == id;
parent = parent || (params.parents.containsKey(ctx._source.doc_id) &&
    params.parents[ctx._source.doc_id].contains(id));
def target = parent ? 0 : params.status;
if (ctx._source.available_int == target) { ctx.op = 'noop'; }
else { ctx._source.available_int = target; }
""",
        "params": {"status": status, "parents": parents},
    }


def complete_availability_update(response: Any) -> bool:
    raw = response.to_dict() if hasattr(response, "to_dict") else getattr(response, "body", response)
    if not isinstance(raw, Mapping) or raw.get("timed_out") is not False or raw.get("failures") != []:
        return False
    counts = [raw.get(key) for key in ("total", "updated", "noops", "version_conflicts")]
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    total, updated, noops, conflicts = counts
    return total > 0 and total == updated + noops and conflicts == 0 and raw.get("deleted", 0) == 0
