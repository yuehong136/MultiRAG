"""Availability-only update scripts and complete bulk acknowledgements."""

from collections.abc import Mapping
from typing import Any


def availability_script(status: int) -> dict[str, Any]:
    return {
        "source": """
def id = ctx._source.containsKey('id') ? ctx._source.id :
    (ctx._source.containsKey('pk') ? ctx._source.pk : ctx._id);
boolean parent = ctx._source.containsKey('mom_id') &&
    ctx._source.mom_id != null && ctx._source.mom_id != '' && ctx._source.mom_id == id;
def target = parent ? 0 : params.status;
if (ctx._source.available_int == target) { ctx.op = 'noop'; }
else { ctx._source.available_int = target; }
""",
        "params": {"status": status},
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
