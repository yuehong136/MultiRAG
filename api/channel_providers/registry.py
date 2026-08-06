"""The one place a provider name is registered.

There used to be two registries with the same name and opposite fates: this
package's ancestor in ``api/channels/core/registry.py`` was never called by
anything (its single registration sat there being ignored, and its builder read
credentials out of a plaintext config dict -- the upstream shape this project
exists to avoid), while ``api/channels/provider.py`` was the live one. A new
provider author reading the code could not tell which was which. That one is
deleted; this is the successor, and ``api/channels/provider.py`` now resolves
its transport module through here so the name list exists exactly once.

Modules are referenced by path and imported lazily on purpose: the transport
half of a provider pulls in an SDK (lark-oapi installs a process-global event
loop), and the control plane must be able to answer "what providers exist"
without paying for that.
"""

from __future__ import annotations

import importlib
from typing import Final

from api.channel_providers.spec import ProviderSpec


class UnknownChannelProvider(LookupError):
    """Raised for a provider name that is not registered. Fails closed."""


# name -> (spec module, transport module). The spec half is pure pydantic and
# safe to import anywhere; the transport half is not.
_PROVIDERS: Final[dict[str, tuple[str, str]]] = {
    "dingtalk": ("api.channel_providers.dingtalk", "api.channels.dingtalk.provider"),
    "feishu": ("api.channel_providers.feishu", "api.channels.feishu.provider"),
}


def provider_names() -> tuple[str, ...]:
    """Registered provider names, in stable order."""

    return tuple(sorted(_PROVIDERS))


def is_registered(name: str) -> bool:
    return name in _PROVIDERS


def provider_spec(name: str) -> ProviderSpec:
    """Import and return one provider's control-plane spec, failing closed."""

    entry = _PROVIDERS.get(name)
    if entry is None:
        raise UnknownChannelProvider(name)
    spec = getattr(importlib.import_module(entry[0]), "PROVIDER_SPEC", None)
    if not isinstance(spec, ProviderSpec) or spec.name != name:
        raise UnknownChannelProvider(name)
    return spec


def provider_specs() -> tuple[ProviderSpec, ...]:
    """Every registered spec, in the same stable order as ``provider_names``."""

    return tuple(provider_spec(name) for name in provider_names())


def transport_module(name: str) -> str:
    """Dotted path to a provider's worker-side descriptor module.

    Returned as a string rather than an imported object so that callers who
    only need the control-plane half never load the SDK.
    """

    entry = _PROVIDERS.get(name)
    if entry is None:
        raise UnknownChannelProvider(name)
    return entry[1]
