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
from typing import Final, NamedTuple

from api.channel_providers.spec import ProviderSpec


class UnknownChannelProvider(LookupError):
    """Raised for a provider name that is not registered. Fails closed."""


class _Registration(NamedTuple):
    """Where one provider's three halves live, by dotted path.

    ``spec`` is pure pydantic and safe to import anywhere. ``transport`` pulls
    in an SDK. ``verify`` is the odd one out: it is imported by the *API*
    process, so it is an SDK-free HTTP probe, and it is optional -- a provider
    that cannot cheaply answer "is this credential good?" simply omits it.

    "Omitted" is the empty string rather than ``None`` on purpose: this module
    runs under ``from __future__ import annotations``, so a ``str | None``
    field here reaches beartype as the *string* ``"str | None"`` and it
    declines to decorate the whole namedtuple. ``verify_module`` translates
    back to ``None`` at the boundary, where the annotation is a real object.
    """

    spec: str
    transport: str
    verify: str = ""


_PROVIDERS: Final[dict[str, _Registration]] = {
    "dingtalk": _Registration(
        spec="api.channel_providers.dingtalk",
        transport="api.channels.dingtalk.provider",
        verify="api.channels.dingtalk.verify",
    ),
    "feishu": _Registration(
        spec="api.channel_providers.feishu",
        transport="api.channels.feishu.provider",
        verify="api.channels.feishu.verify",
    ),
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
    spec = getattr(importlib.import_module(entry.spec), "PROVIDER_SPEC", None)
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
    return entry.transport


def verify_module(name: str) -> str | None:
    """Dotted path to a provider's credential probe, or ``None`` if it has none.

    Same string-not-object reason as ``transport_module``, plus one of its own:
    the probe is optional, and "this provider cannot self-check" has to be
    expressible without inventing a stub module that always fails.
    """

    entry = _PROVIDERS.get(name)
    if entry is None:
        raise UnknownChannelProvider(name)
    return entry.verify or None
