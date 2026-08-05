"""Provider specifications shared by the control plane and the worker.

Pure pydantic: importing this package must not pull in an ORM, a web framework
or a transport SDK. Enforced by an import-linter contract and by
``tests/unit/test_channel_provider_spec.py::test_importing_specs_stays_pure``.
"""

from api.channel_providers.registry import (
    UnknownChannelProvider,
    is_registered,
    provider_names,
    provider_spec,
    provider_specs,
    transport_module,
)
from api.channel_providers.spec import ProviderCapabilities, ProviderSpec, resolve_path

__all__ = [
    "ProviderCapabilities",
    "ProviderSpec",
    "UnknownChannelProvider",
    "is_registered",
    "provider_names",
    "provider_spec",
    "provider_specs",
    "resolve_path",
    "transport_module",
]
