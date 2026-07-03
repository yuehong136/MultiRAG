#
#  Copyright 2026 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#
import json
from datetime import datetime
from functools import cache, lru_cache
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

A2UI_VERSION = "v0.9"
A2UI_CATALOG_ID = "https://a2ui.org/specification/v0_9/basic_catalog.json"
A2UI_EVENT = "a2ui_command"

_SCHEMA_DIR = Path(__file__).with_name("a2ui_schemas")
_COMMON_TYPES_URI = "https://a2ui.org/specification/v0_9/common_types.json"
_SERVER_SCHEMA_URI = "https://a2ui.org/specification/v0_9/server_to_client.json"
_CLIENT_SCHEMA_URI = "https://a2ui.org/specification/v0_9/client_to_server.json"
_CATALOG_ALIAS_URI = "https://a2ui.org/specification/v0_9/catalog.json"

_COMMAND_KEYS = {
    "createSurface",
    "updateComponents",
    "updateDataModel",
    "deleteSurface",
}

_UNSUPPORTED_BASIC_COMPONENT_HINTS = {
    "Form": "Use Column or Row as the form layout container.",
    "TextInput": "Use TextField.",
    "Input": "Use TextField.",
    "Select": "Use ChoicePicker with variant='mutuallyExclusive'.",
    "MultiSelect": "Use ChoicePicker with variant='multipleSelection'.",
    "CheckboxGroup": "Use ChoicePicker with displayStyle='checkbox'.",
    "RadioGroup": "Use ChoicePicker with variant='mutuallyExclusive'.",
}


def parse_a2ui_commands(text: str) -> list[dict[str, Any]]:
    if "```" in text:
        raise ValueError("A2UI node expects raw JSON array or JSONL commands, not fenced markdown")
    return _parse_command_block(text)


def build_a2ui_event(commands: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(commands, list) or not commands:
        raise ValueError("A2UI payload must be a non-empty command list")
    if len(commands) > 50:
        raise ValueError("A2UI payload has too many commands")

    surface_ids = _validate_server_commands(commands)
    return {
        "surface_id": sorted(surface_ids)[0] if len(surface_ids) == 1 else None,
        "surface_ids": sorted(surface_ids),
        "commands": commands,
    }


def validate_client_a2ui_messages(messages: Any) -> list[dict[str, Any]]:
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ValueError("A2UI client messages must be a list")
    if len(messages) > 50:
        raise ValueError("A2UI client messages has too many items")

    validator = _client_validator()
    valid_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("A2UI client message must be an object")
        _validate_with_schema(validator, message, "A2UI client message")
        valid_messages.append(message)
    return valid_messages


def _parse_command_block(text: str) -> list[dict[str, Any]]:
    if not text.strip():
        raise ValueError("A2UI block is empty")

    try:
        payload = json.loads(text)
    except JSONDecodeError:
        payload = None

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        raise ValueError("A2UI block must contain JSONL commands or a JSON command array")

    commands: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            command = json.loads(stripped)
        except JSONDecodeError as exc:
            raise ValueError(f"A2UI JSONL line {index} is invalid: {exc.msg}") from exc
        if not isinstance(command, dict):
            raise ValueError(f"A2UI JSONL line {index} must be an object")
        commands.append(command)

    if not commands:
        raise ValueError("A2UI block must contain at least one command")
    return commands


def _validate_server_commands(commands: list[dict[str, Any]]) -> set[str]:
    validator = _server_validator()
    catalog = _schema("basic_catalog.json")
    component_names = set(catalog.get("components", {}))
    component_validator = _component_validator()

    surface_ids: set[str] = set()
    created_surfaces: set[str] = set()
    components_by_surface: dict[str, dict[str, dict[str, Any]]] = {}
    refs_by_surface: dict[str, set[str]] = {}

    for command in commands:
        if not isinstance(command, dict):
            raise ValueError("A2UI command must be an object")
        if command.get("version") != A2UI_VERSION:
            raise ValueError("A2UI command version must be v0.9")
        if "command" in command:
            raise ValueError(
                "A2UI v0.9 does not use a 'command' field. "
                "Use exactly one top-level command key: createSurface, updateComponents, updateDataModel, or deleteSurface"
            )
        command_keys = [key for key in _COMMAND_KEYS if key in command]
        if len(command_keys) != 1 or len(command) != 2:
            raise ValueError("A2UI command must contain version and exactly one command type")

        command_key = command_keys[0]
        payload = command[command_key]
        surface_id = payload["surfaceId"]
        surface_ids.add(surface_id)

        if command_key == "createSurface":
            _validate_with_schema(validator, command, "A2UI command")
            if payload["catalogId"] != A2UI_CATALOG_ID:
                raise ValueError("A2UI catalogId is not supported")
            created_surfaces.add(surface_id)
            continue

        if surface_id not in created_surfaces:
            raise ValueError("A2UI surface must be created before updates")

        if command_key == "updateComponents":
            _prevalidate_update_components(payload, component_names)

        _validate_with_schema(validator, command, "A2UI command")

        if command_key != "updateComponents":
            path = payload.get("path", "/")
            if command_key == "updateDataModel" and (
                not isinstance(path, str) or not path.startswith("/")
            ):
                raise ValueError("A2UI updateDataModel requires a JSON Pointer path")
            continue

        surface_components = components_by_surface.setdefault(surface_id, {})
        surface_refs = refs_by_surface.setdefault(surface_id, set())
        for component in payload["components"]:
            component_id = component["id"]
            component_name = component["component"]
            _validate_with_schema(component_validator, component, "A2UI component")
            surface_components[component_id] = component
            surface_refs.update(_collect_component_refs(component))

    for surface_id in created_surfaces:
        components = components_by_surface.get(surface_id, {})
        root_count = 1 if "root" in components else 0
        if root_count != 1:
            raise ValueError("A2UI surface must define exactly one root component")
        missing_refs = sorted(ref for ref in refs_by_surface.get(surface_id, set()) if ref not in components)
        if missing_refs:
            raise ValueError("A2UI children reference unknown component ids")

    if not surface_ids:
        raise ValueError("A2UI payload must include at least one surfaceId")
    return surface_ids


def _prevalidate_update_components(payload: dict[str, Any], component_names: set[str]) -> None:
    components = payload.get("components")
    if not isinstance(components, list):
        return

    component_ids: set[str] = set()
    allowed = ", ".join(sorted(component_names))

    for component in components:
        if not isinstance(component, dict):
            continue

        component_id = component.get("id")
        if isinstance(component_id, str):
            if component_id in component_ids:
                raise ValueError("A2UI component ids must be unique in a command block")
            component_ids.add(component_id)

        component_name = component.get("component")
        if isinstance(component_name, str) and component_name not in component_names:
            hint = _UNSUPPORTED_BASIC_COMPONENT_HINTS.get(component_name)
            suffix = f" {hint}" if hint else ""
            raise ValueError(
                f"A2UI component '{component_name}' is not in the official Basic Catalog. "
                f"Allowed components: {allowed}.{suffix}"
            )

        if component_name == "CheckBox" and "label" not in component:
            component_id_text = f" '{component_id}'" if isinstance(component_id, str) else ""
            raise ValueError(f"A2UI CheckBox component{component_id_text} must include a label")

        for key in ("child", "trigger", "content"):
            if isinstance(component.get(key), dict):
                raise ValueError(f"A2UI component '{key}' must reference a component id, not an inline component")

        children = component.get("children")
        if isinstance(children, list) and any(isinstance(child, dict) for child in children):
            raise ValueError("A2UI component children must reference component ids, not inline components")

        tabs = component.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and isinstance(tab.get("child"), dict):
                    raise ValueError("A2UI tab child must reference a component id, not an inline component")


def _collect_component_refs(component: dict[str, Any]) -> set[str]:
    refs: set[str] = set()

    child = component.get("child")
    if isinstance(child, str):
        refs.add(child)

    children = component.get("children")
    if isinstance(children, list):
        refs.update(item for item in children if isinstance(item, str))
    elif isinstance(children, dict):
        component_id = children.get("componentId")
        if isinstance(component_id, str):
            refs.add(component_id)

    tabs = component.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, dict) and isinstance(tab.get("child"), str):
                refs.add(tab["child"])

    for key in ("trigger", "content"):
        value = component.get(key)
        if isinstance(value, str):
            refs.add(value)

    return refs


def _validate_with_schema(
    validator: Draft202012Validator,
    value: dict[str, Any],
    label: str,
) -> None:
    try:
        validator.validate(value)
    except ValidationError as exc:
        path = "/" + "/".join(str(part) for part in exc.absolute_path)
        message = exc.message
        raise ValueError(f"{label} schema validation failed at {path}: {message}") from exc


@cache
def _schema(filename: str) -> dict[str, Any]:
    path = _SCHEMA_DIR / filename
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


@lru_cache(maxsize=1)
def _schema_registry() -> Registry:
    common = _schema("common_types.json")
    catalog = _schema("basic_catalog.json")
    server = _schema("server_to_client.json")
    client = _schema("client_to_server.json")
    return Registry().with_resources(
        [
            (_COMMON_TYPES_URI, _resource(common)),
            (_CATALOG_ALIAS_URI, _resource(catalog)),
            (A2UI_CATALOG_ID, _resource(catalog)),
            (_SERVER_SCHEMA_URI, _resource(server)),
            (_CLIENT_SCHEMA_URI, _resource(client)),
            ("common_types.json", _resource(common)),
            ("catalog.json", _resource(catalog)),
            ("basic_catalog.json", _resource(catalog)),
            ("server_to_client.json", _resource(server)),
            ("client_to_server.json", _resource(client)),
        ]
    )


def _resource(contents: dict[str, Any]) -> Resource:
    return Resource.from_contents(contents, default_specification=DRAFT202012)


@lru_cache(maxsize=1)
def _server_validator() -> Draft202012Validator:
    return Draft202012Validator(_schema("server_to_client.json"), registry=_schema_registry())


@lru_cache(maxsize=1)
def _client_validator() -> Draft202012Validator:
    return Draft202012Validator(_schema("client_to_server.json"), registry=_schema_registry())


@lru_cache(maxsize=1)
def _component_validator() -> Draft202012Validator:
    catalog = _schema("basic_catalog.json")
    component_refs = [
        {"$ref": f"{A2UI_CATALOG_ID}#/components/{component_name}"}
        for component_name in catalog.get("components", {})
    ]
    return Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "oneOf": component_refs,
        },
        registry=_schema_registry(),
    )


def validate_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
