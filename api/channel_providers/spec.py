"""What a channel provider *is*, described once for everyone who needs it.

Deliberately dependency-free: no ORM, no web framework, no transport SDK. The
control plane owns the database and the worker process owns the SDK, and they
each deliberately refuse the other's dependencies -- so anything both of them
need to agree on has to live somewhere neither of them taints. That boundary is
enforced by an import-linter contract plus a subprocess purity test, because
import-linter alone cannot express "no third-party SDK".

Specs must not live under ``api/channels/<name>/``: importing one would drag in
that package's ``__init__``, which eagerly imports the provider SDK (lark-oapi
installs a process-global event loop). The control plane resolving a manifest
must not pay that cost.

See CHN-ADR-03 for why rendering is driven by a server-flattened field list
rather than by the JSON Schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ProviderCapabilities(BaseModel):
    """What kinds of message traffic a provider can carry.

    Distinct from how its credential is obtained -- that is onboarding, a
    different axis, and it does not exist yet (CHN-P12).
    """

    private_chat: bool
    group_chat: bool
    text: bool
    files: bool
    images: bool
    streaming_cards: bool


def resolve_path(source: Any, path: str) -> Any:
    """Read a dotted path out of nested mappings, or None if absent.

    Provider config is nested (``credential.app_id``), and every site that
    needs to reach into it -- the account-identity check, the secret split,
    the enable preconditions -- would otherwise hand-roll the same traversal.
    """

    current = source
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One provider's control-plane description.

    Pure data. The transport half of a provider (its SDK wiring, its worker
    tuning) is described separately by ``api.channels.provider.WorkerProvider``
    and is imported lazily, because it costs a process-global event loop.
    """

    name: str
    display_name: str
    capabilities: ProviderCapabilities

    # Request models. ``config_model`` validates a create; ``config_patch_model``
    # a merge-patch, where every field is optional so "absent" keeps the stored
    # value. This is why required-ness cannot be read off the JSON Schema and
    # has to be stated in the form layer instead (CHN-P2).
    config_model: type[BaseModel]
    config_patch_model: type[BaseModel]

    # Dotted paths inside a validated config whose values belong in the
    # encrypted secret store and must never reach the public config column.
    secret_paths: frozenset[str]

    # Dotted path to the provider account this channel connects as. Two enabled
    # channels of one tenant on the same account would both connect and answer
    # every message twice, so the control plane rejects the second (CHN-S4).
    account_identity_path: str

    def account_identity(self, public_config: Any) -> str | None:
        """The provider account a channel connects as, or None if unset."""

        value = resolve_path(public_config, self.account_identity_path)
        return value if isinstance(value, str) and value else None
