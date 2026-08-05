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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Widget kinds the management form knows how to render. Deliberately an open
# union on the wire: a client that meets an unknown kind must render a disabled
# field showing its label rather than throwing, which is what lets the server
# introduce a control type without a coordinated frontend release. That is also
# the entire seam cost of deferring interactive pairing (CHN-P12).
FieldKind = Literal["text", "password", "string_list", "select", "switch"]


class FieldOption(BaseModel):
    """One choice of a ``select`` field."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str


class FormField(BaseModel):
    """One rendered input, already flattened out of the nested config shape.

    ``required`` lives here rather than in the JSON Schema on purpose. Every
    provider config field carries a default so that PATCH can mean "merge",
    which makes the schema's ``required`` array disappear entirely -- the
    frontend was hardcoding a required set to compensate, and hardcoding it
    *wrongly*, since the schema said everything was optional. Stating it in the
    form layer lets both halves be true at once. See CHN-ADR-03.
    """

    model_config = ConfigDict(frozen=True)

    # Dotted path into the config object, e.g. ``credential.app_id``. The client
    # reassembles the nested payload from these, so it never needs to know a
    # provider's shape.
    path: str
    kind: FieldKind
    # Server-owned English default. The client prefers its own translation via
    # ``i18n_key`` and falls back to this, so a new provider is usable before
    # anyone writes locale entries for it.
    label: str
    i18n_key: str | None = None
    required: bool = False
    # Blank means "keep what is stored" -- the server never echoes a secret, so
    # a blank secret input cannot be distinguished from an unchanged one.
    secret: bool = False
    placeholder: str | None = None
    help_text: str | None = None
    default: str | bool | list[str] | None = None
    options: list[FieldOption] | None = None
    max_length: int | None = None
    max_items: int | None = None


class ProviderForm(BaseModel):
    """The ordered field list a client renders for one provider."""

    model_config = ConfigDict(frozen=True)

    # Bumped only when an older client would render this form *wrongly* --
    # changed path semantics, or a field it must honour and cannot know about.
    #
    # Adding fields, kinds or options never bumps it: an unknown ``kind``
    # already renders as a disabled input rather than throwing, and unknown
    # keys are ignored, so additive change costs no coordination at all.
    #
    # The flip side is the contract a bump buys: a client that does not know
    # this version must refuse to render the provider, not guess. See
    # ``SUPPORTED_FORM_VERSION`` in ``web:src/api/channel.ts``, which drops such
    # a manifest into the same "providers unavailable" path as a missing form.
    version: int = 1
    fields: list[FormField] = Field(default_factory=list)


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

    # The render contract: an ordered, flattened field list. The client sorts
    # nothing, resolves no $ref and evaluates no JSON Schema keyword — it maps
    # each entry to a widget and reassembles the nested payload from the paths.
    form: ProviderForm

    # Request models. ``config_model`` validates a create; ``config_patch_model``
    # a merge-patch, where every field is optional so "absent" keeps the stored
    # value. This is why required-ness cannot be read off the JSON Schema and
    # has to be stated in the form layer instead (CHN-P2).
    config_model: type[BaseModel]
    config_patch_model: type[BaseModel]

    # Every path that makes up this provider's credential, secret or not. A
    # credential is split across two stores -- the secret half encrypted, the
    # non-secret half (an app id, a corp id) legitimately in the public config
    # -- and the worker is handed it reassembled. Declaring the whole set is
    # what lets that reassembly be generic instead of pattern-matching on a
    # ``credential.`` prefix.
    credential_paths: frozenset[str]

    # The subset of ``credential_paths`` that belongs in the encrypted secret
    # store and must never reach the public config column.
    secret_paths: frozenset[str]

    # Dotted path to the provider account this channel connects as. Two enabled
    # channels of one tenant on the same account would both connect and answer
    # every message twice, so the control plane rejects the second (CHN-S4).
    account_identity_path: str

    def account_identity(self, public_config: Any) -> str | None:
        """The provider account a channel connects as, or None if unset."""

        value = resolve_path(public_config, self.account_identity_path)
        return value if isinstance(value, str) and value else None
