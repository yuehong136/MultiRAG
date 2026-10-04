"""Explicit protocol selection; never infer a protocol from request bodies."""

import os

ASSETS_PROTOCOL = "multirag-assets-v1"
CORE_PROTOCOL = "ragflow-skills-v1"


def selected_protocol() -> str:
    value = os.environ.get("SKILLS_API_PROTOCOL", ASSETS_PROTOCOL)
    if value not in {ASSETS_PROTOCOL, CORE_PROTOCOL}:
        raise RuntimeError("SKILLS_API_PROTOCOL must be multirag-assets-v1 or ragflow-skills-v1")
    return value


PROTOCOL = selected_protocol()


def response_protocol(path: str) -> str:
    if "/skill-assets/" in path or path.endswith("/skill-assets"):
        return ASSETS_PROTOCOL
    if "/skill-core/" in path or path.endswith("/skill-core"):
        return CORE_PROTOCOL
    return PROTOCOL
