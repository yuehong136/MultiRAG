"""Strict private-API models for independent Channel runtimes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Provider names are an application-level set, deliberately not a Literal here.
# Pinning this to one provider made a single unrecognised row fail the whole
# response, and the supervisor treats that as "skip this entire reconcile tick"
# -- so one bad row stopped every binding, including healthy ones, from being
# started or reaped. The runner still fails closed on names it cannot resolve
# (`api/channels/provider.py::worker_provider`), which is where that check
# belongs. See CHN-ADR-06 for why this widening ships well before anything
# emits a second provider name.
ProviderName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


class DesiredRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1, max_length=32)
    provider: ProviderName
    generation: int = Field(ge=1)


class DesiredRuntimeList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DesiredRuntime]


class RuntimeCredential(BaseModel):
    """Credential returned only through the authenticated private route.

    Provider-agnostic: keyed by the leaf names a provider spec declares in
    ``credential_paths``, which is what lets a second provider exist without
    the runtime route naming any of its fields.

    **CHN-P11 completed the three-step field removal here.** ``app_id`` and
    ``app_secret`` — Feishu's names, sitting in a model every provider has to
    share — went stop-reading (CHN-P4), stop-emitting (CHN-P8), drop (this).
    Each step shipped and was deployed on its own, because these models are
    ``extra="forbid"`` and the supervisor is a long-lived process an API deploy
    does not restart; see CHN-ADR-06. Anything added here later owes the same
    three steps, in the same order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: dict[str, str] = Field(default_factory=dict, json_schema_extra={"writeOnly": True})

    def value(self, key: str) -> str:
        """Read one credential value, or an empty string when it is absent.

        Absent and empty deliberately collapse: a provider descriptor checks
        the result for truthiness and raises its own classified error, which
        keeps "no such key" and "key present but blank" from becoming two
        indistinguishable failure paths in every provider.
        """

        return self.fields.get(key) or ""


class RuntimeBindingConfig(BaseModel):
    """Everything one runner needs to serve exactly one binding.

    **Tolerate half of a two-step contract change (CHN-O2 → CHN-O3).** ``policy``
    is accepted here and read by the runner, but the API emits nothing into it
    yet and the route strips it from the wire. The emit half only ships once
    every supervisor and worker runs a build containing this model. See
    CHN-ADR-06, including the clause CHN-P4 added: a tolerate step that changes
    the wire is not a tolerate step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1, max_length=32)
    provider: ProviderName
    generation: int = Field(ge=1)
    public_config: dict[str, Any]
    credential: RuntimeCredential

    # Free-form on purpose, mirroring the column: an admin's binding policy is
    # validated for shape and for carrying no credentials, not against a closed
    # key set. A typed extra="forbid" model here would reject a policy the
    # control plane happily stored, which is the wrong place to discover it.
    policy: dict[str, Any] = Field(default_factory=dict)

    @property
    def private_chat_only(self) -> bool:
        """Whether the runner must ignore anything but one-to-one chats.

        Defaults to ``True``, which is both today's hardcoded behaviour and the
        fail-safe direction: during the tolerate window the API sends no policy
        at all, and the wrong default would silently let a bot start answering
        in every group chat it sits in. A non-boolean value is treated the same
        way -- a malformed policy must not widen exposure.
        """

        value = self.policy.get("private_chat_only", True)
        return value if isinstance(value, bool) else True


RuntimeState = Literal["waiting", "starting", "connected", "stopping", "stopped", "error"]


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observed_generation: int = Field(ge=0)
    state: RuntimeState
    runner_id: str | None = Field(default=None, min_length=1, max_length=128)
    connected_at: datetime | None = None
    last_error_code: str | None = Field(default=None, min_length=1, max_length=64)
