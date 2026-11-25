"""The gateway's view of an owner's registered models: which are open, and by which names.

There is no publishing step. Every registered model of a connected provider is
in the directory. ``GatewayModelSetting`` rows hold what an operator changed
(switch, alias, preference, limits); a model nobody touched follows the
owner's ``GatewaySetting.new_models_enabled``.

Names. Each model answers to, in order of precedence when strings collide:

1. its alias, when set;
2. ``Provider/instance/model`` — always unique, so every model has a stable name;
3. ``Provider/model`` — owned by one model per provider and model name;
4. the bare model name — owned by one model per model name.

For 3 and 4 the ``preferred`` model owns the name, otherwise the earliest
registered. All registered models take part, open or not, so opening, closing
or adding a model never moves a name another model already answers to.

Deployments. The owner is the model a shared name is listed under; a call to
that name may be served by any model that has the same name at the same level
— the same alias, the same ``Provider/model`` or the same bare name. Those
models are the name's deployments (LiteLLM's model group) and the router picks
among them. ``Provider/instance/model`` always means exactly one model.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import GatewayModelSetting, GatewaySetting
from api.model_gateway import catalog
from api.model_gateway.catalog import SUPPORTED_MODEL_TYPES, Connection, RegisteredModel
from api.model_gateway.limits import NO_LIMITS, Limits
from api.model_gateway.pricing import ModelPrice, custom_price

CALLABLE = "callable"
CLOSED = "closed"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class NameSource:
    """What naming needs to know about one model."""

    model_id: str
    registration_order: int
    provider: str
    instance_name: str
    model_name: str
    alias: str | None = None
    preferred: bool = False


def assign_names(sources: Iterable[NameSource]) -> dict[str, tuple[str, ...]]:
    """Every name each model answers to, its primary (call) name first.

    The primary name is the alias, else the bare name, else ``Provider/model``,
    else ``Provider/instance/model`` — the first of those the model owns.
    """
    return _name_table(sources)[0]


def name_groups(sources: Iterable[NameSource]) -> dict[str, tuple[str, ...]]:
    """The models behind each name, its owner first."""
    return _name_table(sources)[1]


def _name_table(sources: Iterable[NameSource]) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    ordered = sorted(sources, key=lambda source: (not source.preferred, source.registration_order))
    owner: dict[str, str] = {}
    owner_tier: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    claims: dict[str, dict[str, str]] = {source.model_id: {} for source in ordered}
    tiers = (
        ("alias", lambda source: source.alias),
        ("full", lambda source: f"{source.provider}/{source.instance_name}/{source.model_name}"),
        ("provider", lambda source: f"{source.provider}/{source.model_name}"),
        ("bare", lambda source: source.model_name),
    )
    for tier, name_of in tiers:
        for source in ordered:
            name = name_of(source)
            if not name:
                continue
            if name not in owner:
                owner[name], owner_tier[name] = source.model_id, tier
                claims[source.model_id][tier] = name
            if owner_tier[name] == tier:
                members.setdefault(name, []).append(source.model_id)
    names: dict[str, tuple[str, ...]] = {}
    for model_id, owned in claims.items():
        primary = [owned[tier] for tier in ("alias", "bare", "provider", "full") if tier in owned]
        names[model_id] = tuple(dict.fromkeys(primary))
    return names, {name: tuple(ids) for name, ids in members.items()}


@dataclass(frozen=True, slots=True)
class DirectoryModel:
    """One registered model with its gateway state and names."""

    model_id: str
    model_name: str
    provider: str
    instance_id: str
    instance_name: str
    model_types: tuple[str, ...]
    # Capabilities the gateway has an endpoint for; empty when none.
    servable_types: tuple[str, ...]
    # Why the gateway cannot serve it right now, whatever its switch says.
    reason: str | None
    enabled: bool
    # True while no one set the switch: it follows the owner's default.
    enabled_by_default: bool
    alias: str | None
    preferred: bool
    description: str | None
    limits: Limits
    names: tuple[str, ...]
    shares_name: bool
    created: int
    # The operator's price; ``None`` leaves the model to LiteLLM's cost map.
    custom_price: ModelPrice | None = None
    # Routing among the deployments of a shared name, as in LiteLLM.
    weight: int = 1
    order: int | None = None
    # Names this model serves together with other callable models: (name, deployments).
    pools: tuple[tuple[str, int], ...] = ()

    @property
    def call_name(self) -> str:
        return self.names[0]

    @property
    def state(self) -> str:
        if self.reason is not None:
            return UNAVAILABLE
        return CALLABLE if self.enabled else CLOSED


@dataclass(frozen=True, slots=True)
class Directory:
    new_models_enabled: bool
    connections: tuple[Connection, ...]
    models: tuple[DirectoryModel, ...]
    _by_name: dict[str, DirectoryModel] = field(init=False, repr=False, compare=False)
    _by_id: dict[str, DirectoryModel] = field(init=False, repr=False, compare=False)
    # Every model behind each name, the owner first.
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {name: model for model in self.models for name in model.names})
        object.__setattr__(self, "_by_id", {model.model_id: model for model in self.models})

    def lookup(self, name: str) -> DirectoryModel | None:
        return self._by_name.get(name)

    def model(self, model_id: str) -> DirectoryModel | None:
        return self._by_id.get(model_id)

    def deployments(self, name: str) -> tuple[DirectoryModel, ...]:
        """The models a call to ``name`` may be served by, open or not; the owner first."""
        return tuple(self._by_id[model_id] for model_id in self.groups.get(name, ()))


def _reason(model: RegisteredModel, servable: Sequence[str]) -> str | None:
    if not model.relayable:
        return "provider_not_supported"
    if not servable:
        return "endpoint_not_supported"
    if not model.available:
        return "model_disabled"
    return None


def build_directory(
    registered: Sequence[RegisteredModel],
    settings: Mapping[str, GatewayModelSetting],
    new_models_enabled: bool,
    connections: Sequence[Connection] = (),
) -> Directory:
    """Combine registrations, per-model settings and the owner default. Pure."""
    names, groups = _name_table(
        NameSource(
            model_id=model.model_id,
            registration_order=model.registration_order,
            provider=model.provider,
            instance_name=model.instance_name,
            model_name=model.model_name,
            alias=settings[model.model_id].alias if model.model_id in settings else None,
            preferred=bool(settings[model.model_id].preferred) if model.model_id in settings else False,
        )
        for model in registered
    )
    name_counts: dict[str, int] = {}
    for model in registered:
        name_counts[model.model_name] = name_counts.get(model.model_name, 0) + 1
    models = []
    for model in registered:
        setting = settings.get(model.model_id)
        servable = tuple(kind for kind in model.model_types if kind in SUPPORTED_MODEL_TYPES and kind in model.relay_types)
        models.append(
            DirectoryModel(
                model_id=model.model_id,
                model_name=model.model_name,
                provider=model.provider,
                instance_id=model.instance_id,
                instance_name=model.instance_name,
                model_types=model.model_types,
                servable_types=servable,
                reason=_reason(model, servable),
                enabled=bool(setting.enabled) if setting is not None else new_models_enabled,
                enabled_by_default=setting is None,
                alias=setting.alias if setting is not None else None,
                preferred=bool(setting.preferred) if setting is not None else False,
                description=setting.description if setting is not None else None,
                limits=Limits(rpm=setting.rpm_limit, tpm=setting.tpm_limit, max_parallel=setting.max_parallel_requests) if setting is not None else NO_LIMITS,
                names=names[model.model_id],
                shares_name=name_counts[model.model_name] > 1,
                created=model.created,
                custom_price=custom_price(setting.input_cost_per_token, setting.output_cost_per_token, setting.cache_read_input_token_cost) if setting is not None else None,
                weight=setting.weight if setting is not None and setting.weight is not None else 1,
                order=setting.deployment_order if setting is not None else None,
            )
        )
    callable_ids = {model.model_id for model in models if model.state == CALLABLE}
    pools: dict[str, list[tuple[str, int]]] = {}
    for name, ids in groups.items():
        serving = [model_id for model_id in ids if model_id in callable_ids]
        if len(serving) > 1:
            for model_id in serving:
                pools.setdefault(model_id, []).append((name, len(serving)))
    models = [replace(model, pools=tuple(pools[model.model_id])) if model.model_id in pools else model for model in models]
    return Directory(new_models_enabled=new_models_enabled, connections=tuple(connections), models=tuple(models), groups=groups)


# Settings change through bulk statements in the same transaction; always read the rows back.
_FRESH = {"populate_existing": True}


async def tenant_setting(db: AsyncSession, tenant_id: str) -> GatewaySetting | None:
    return await db.scalar(select(GatewaySetting).where(GatewaySetting.tenant_id == tenant_id).execution_options(**_FRESH))


async def model_settings(db: AsyncSession, tenant_id: str) -> dict[str, GatewayModelSetting]:
    rows = (await db.scalars(select(GatewayModelSetting).where(GatewayModelSetting.tenant_id == tenant_id).execution_options(**_FRESH))).all()
    return {row.model_id: row for row in rows}


async def load_directory(db: AsyncSession, tenant_id: str, *, with_connections: bool = False) -> Directory:
    """The owner's directory, read in the caller's session."""
    setting = await tenant_setting(db, tenant_id)
    return build_directory(
        await catalog.registered_models(db, tenant_id),
        await model_settings(db, tenant_id),
        setting.new_models_enabled if setting is not None else True,
        await catalog.connections(db, tenant_id) if with_connections else (),
    )
