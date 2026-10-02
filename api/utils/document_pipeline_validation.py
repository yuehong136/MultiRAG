"""Validate the actual Graph component parameters without constructing a run."""

import copy
import re
from typing import Any


def validate_document_pipeline(dsl: Any) -> None:
    from agent.component import component_class
    from agent.component.base import ComponentBase
    from agent.dsl_migration import normalize_chunker_dsl

    if not isinstance(dsl, dict) or not isinstance(dsl.get("components"), dict) or not dsl["components"]:
        raise ValueError("Invalid document pipeline definition.")
    definition = normalize_chunker_dsl(copy.deepcopy(dsl))
    components = definition["components"]
    root = components.get("File")
    if not isinstance(root, dict) or not isinstance(root.get("obj"), dict) or root["obj"].get("component_name") != "File":
        raise ValueError("Document pipeline requires its File source root.")
    if root.get("upstream", []):
        raise ValueError("Document pipeline source cannot have an upstream edge.")
    path = definition.get("path", [])
    if not isinstance(path, list) or any(not isinstance(identifier, str) or identifier not in components for identifier in path):
        raise ValueError("Invalid document pipeline path.")
    if path:
        # Pipeline.run skips File for a saved execution path, while Graph.load
        # constructs fresh components without that run's outputs. Saving such
        # a definition would read stale/missing inputs for the new document.
        raise ValueError("Document pipeline definition contains a saved execution path.")

    def references(value: Any) -> None:
        if isinstance(value, str):
            for match in re.finditer(ComponentBase.variable_ref_patt, value):
                reference = match.group(1)
                if "@" in reference and reference.split("@", 1)[0] not in components:
                    raise ValueError("Invalid document pipeline variable reference.")
        elif isinstance(value, dict):
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)

    for identifier, component in components.items():
        if not isinstance(identifier, str) or not isinstance(component, dict) or not isinstance(component.get("obj"), dict):
            raise ValueError("Invalid document pipeline component.")
        obj = component["obj"]
        name, params = obj.get("component_name"), obj.get("params")
        if not isinstance(name, str) or name == "Begin" or not isinstance(params, dict):
            raise ValueError("Invalid document pipeline component parameters.")
        for direction in ["upstream", "downstream"]:
            edges = component.get(direction)
            if not isinstance(edges, list) or any(not isinstance(edge, str) or edge not in components for edge in edges):
                raise ValueError("Invalid document pipeline edge.")
        if component.get("parent_id") and component["parent_id"] not in components:
            raise ValueError("Invalid document pipeline parent.")
        cls = component_class(name)
        if not isinstance(cls, type) or not issubclass(cls, ComponentBase):
            raise ValueError("Unsupported document pipeline component.")
        param = component_class(name + "Param")()
        param.update({**params, "custom_header": None})
        param.check()
        references(params)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError("Document pipeline contains a nonterminating edge cycle.")
        if identifier in visited:
            return
        visiting.add(identifier)
        for edge in components[identifier].get("downstream", []):
            visit(edge)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in components:
        visit(identifier)
