"""Request bodies of the model gateway management API."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

_MAX_TAGS = 16
_MAX_TAG_KEY = 64
_MAX_TAG_VALUE = 256
_MAX_ALLOWED_MODELS = 200

PublicModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")]
KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Description = Annotated[str, StringConstraints(max_length=2000)]
# Rerank joins once its per-provider upstream path is covered by the relay.
ServedModelType = Literal["chat", "embedding"]


def _valid_tags(tags: dict[str, str] | None) -> dict[str, str] | None:
    if tags is None:
        return None
    if len(tags) > _MAX_TAGS:
        raise ValueError(f"at most {_MAX_TAGS} tags are allowed")
    for name, value in tags.items():
        if not name or len(name) > _MAX_TAG_KEY or len(value) > _MAX_TAG_VALUE:
            raise ValueError(f"tag names are 1-{_MAX_TAG_KEY} characters and values at most {_MAX_TAG_VALUE}")
    return tags


def _future_instant(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("expires_at must include a time zone offset")
    if value <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return value


Tags = Annotated[dict[str, str] | None, AfterValidator(_valid_tags)]
Expiry = Annotated[datetime | None, AfterValidator(_future_instant)]
AllowedModelIds = Annotated[list[str], Field(max_length=_MAX_ALLOWED_MODELS)]


class _Body(BaseModel):
    # ``model_id`` and ``model_type`` are domain names here, not pydantic internals.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class GatewayModelCreate(_Body):
    name: PublicModelName
    model_id: int = Field(gt=0)
    model_type: ServedModelType
    description: Description | None = None


class GatewayModelPatch(_Body):
    name: PublicModelName | None = None
    description: Description | None = None
    status: Literal["active", "inactive"] | None = None


class GatewayKeyCreate(_Body):
    name: KeyName
    description: Description | None = None
    tags: Tags = None
    expires_at: Expiry = None
    allow_all_models: bool = False
    allowed_model_ids: AllowedModelIds = Field(default_factory=list)


class GatewayKeyPatch(_Body):
    name: KeyName | None = None
    description: Description | None = None
    tags: Tags = None
    expires_at: Expiry = None
    allow_all_models: bool | None = None
    allowed_model_ids: AllowedModelIds | None = None
    status: Literal["active", "disabled"] | None = None
