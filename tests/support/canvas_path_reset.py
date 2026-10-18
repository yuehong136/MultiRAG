"""A provider-free workflow with two real UserFillUp pause points."""

from typing import Any


def paused_dsl() -> dict[str, Any]:
    nodes = ["begin", "Message:draft", "UserFillUp:first", "Message:approved", "UserFillUp:second", "Message:answer"]
    params: list[dict[str, Any]] = [
        {"prologue": "Start"},
        {"content": ["Draft:{sys.query}"]},
        {"inputs": {"approval": {"type": "line", "name": "Approval"}}, "tips": "Approve {Message:draft@content}"},
        {"content": ["{Message:draft@content}/{UserFillUp:first@approval}"]},
        {"inputs": {"delivery": {"type": "line", "name": "Delivery"}}, "tips": "Deliver {Message:approved@content}"},
        {"content": ["{Message:draft@content}/{UserFillUp:first@approval}/{Message:approved@content}/{UserFillUp:second@delivery}"]},
    ]
    components = {}
    for index, (node, param) in enumerate(zip(nodes, params, strict=True)):
        components[node] = {
            "obj": {"component_name": "Begin" if node == "begin" else node.split(":")[0], "params": param},
            "upstream": nodes[index - 1 : index] if index else [],
            "downstream": nodes[index + 1 : index + 2],
        }
    # This node has not run, but a saved output must survive ordinary turns.
    components["Message:unvisited"] = {
        "obj": {"component_name": "Message", "params": {"content": ["Never run"], "stream": False, "outputs": {"content": {"type": "str", "value": "saved branch"}}}},
        "upstream": [],
        "downstream": [],
    }
    return {"components": components, "path": [], "history": [], "retrieval": []}
