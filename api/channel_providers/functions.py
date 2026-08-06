"""Generic config handling, driven by a provider's declared paths.

Everything here used to be Feishu-specific code in the control plane: which
keys are credentials, where the account identifier lives, what a merge-patch
touches. Reading it off the spec instead is what makes "add a provider" stop
meaning "edit the control plane".
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from api.channel_providers.spec import ProviderSpec


class ProviderConfigInvalid(ValueError):
    """A config payload the declaring provider refuses.

    Carries only field locations and pydantic's own messages -- never the
    submitted values. ``ValidationError.errors()`` includes an ``input`` key,
    and echoing it would put a rejected ``app_secret`` straight into an API
    error body and the logs that carry it.
    """


def validate_config(spec: ProviderSpec, payload: Mapping[str, Any], *, partial: bool = False) -> BaseModel:
    """Parse a raw config payload with the model its provider declares.

    The request models cannot do this themselves: a PATCH body says nothing
    about which provider it is for -- only the stored row knows -- so the type
    on the wire is an open object and the real check happens here, dispatched
    on the provider. Strictness is unchanged; it just stopped being one
    provider's model hardcoded into the control plane's request schema.
    """

    model = spec.config_patch_model if partial else spec.config_model
    try:
        return model.model_validate(dict(payload))
    except ValidationError as error:
        details = "; ".join(f"{'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}" for item in error.errors())
        raise ProviderConfigInvalid(f"{spec.name}: {details}") from None


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _drop_nulls(value: Any) -> Any:
    """Recursively drop None-valued keys.

    Unset optional fields are absent from the public config rather than present
    as null. That is the shape the column already holds, and preserving it
    keeps this a refactor -- a stored ``{"credential": {}}`` must not silently
    become ``{"credential": {"app_id": null}}``.
    """

    if isinstance(value, dict):
        return {key: _drop_nulls(nested) for key, nested in value.items() if nested is not None}
    if isinstance(value, list):
        return [_drop_nulls(item) for item in value]
    return value


def _pop_path(target: dict[str, Any], path: str) -> Any:
    """Remove a dotted path from a nested dict and return what was there."""

    parts = path.split(".")
    cursor: Any = target
    for part in parts[:-1]:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    if not isinstance(cursor, dict):
        return None
    return cursor.pop(parts[-1], None)


def _read_path(source: Any, path: str) -> Any:
    cursor = source
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _write_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = target
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = value


def _explicit_paths(model: BaseModel, prefix: str = "") -> dict[str, Any]:
    """Dotted paths the caller actually set, with their values.

    A merge-patch distinguishes "set to null" from "not mentioned", and pydantic
    records that in ``model_fields_set`` per model -- so a nested patch has to
    be walked level by level to recover the full picture.
    """

    explicit: dict[str, Any] = {}
    for name in model.model_fields_set:
        value = getattr(model, name, None)
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            explicit.update(_explicit_paths(value, f"{path}."))
        else:
            explicit[path] = value
    return explicit


def _ensure_parents(target: dict[str, Any], spec: ProviderSpec) -> None:
    """Materialise the object prefixes the provider declares.

    A channel with no credential yet still reports ``{"credential": {}}`` rather
    than omitting the key, because that is what the read path has always
    returned and the client renders from it.
    """

    for field in spec.form.fields:
        parts = field.path.split(".")
        cursor = target
        for part in parts[:-1]:
            nested = cursor.get(part)
            if not isinstance(nested, dict):
                nested = {}
                cursor[part] = nested
            cursor = nested


def split_config(spec: ProviderSpec, config: BaseModel) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Split one validated config into public settings and plaintext secrets.

    Dumped in ``mode="python"``, never ``mode="json"``: pydantic renders a
    SecretStr as the literal string ``'**********'`` in json mode, and that
    string would travel into the public config column looking exactly like a
    real value -- passing every check that looks for a *missing* credential.

    Secrets are keyed by leaf name (``app_secret``), matching what the secret
    store already holds. Changing that key would make existing ciphertext
    unreadable.
    """

    dumped = config.model_dump(mode="python")
    plaintext: dict[str, str] = {}

    for path in sorted(spec.secret_paths):
        value = _pop_path(dumped, path)
        if isinstance(value, SecretStr):
            plaintext[_leaf(path)] = value.get_secret_value()

    public = _drop_nulls(dumped)
    _ensure_parents(public, spec)
    return public, plaintext or None


def merge_config_patch(
    spec: ProviderSpec,
    current: dict[str, Any],
    patch: BaseModel,
    *,
    sanitize: Any,
) -> tuple[dict[str, Any], dict[str, str] | None, bool]:
    """Apply a merge-patch to a stored public config.

    ``sanitize`` is injected rather than imported: it belongs to the control
    plane (it is a defensive backstop over whatever a legacy row happens to
    hold), and this package must not depend on the control plane.

    Returns the new public config, any plaintext secrets to re-encrypt, and
    whether the public half actually changed -- callers use that to decide
    whether the runtime generation has to advance.
    """

    public = sanitize(deepcopy(current))
    _ensure_parents(public, spec)
    plaintext: dict[str, str] = {}
    changed = False

    for path, value in _explicit_paths(patch).items():
        if value is None:
            # Explicitly null still means "leave it alone" here: clearing a
            # field is a separate action, not a side effect of omitting it.
            continue
        if path in spec.secret_paths:
            if isinstance(value, SecretStr):
                plaintext[_leaf(path)] = value.get_secret_value()
            continue
        normalized = list(value) if isinstance(value, list) else value
        if _read_path(public, path) != normalized:
            _write_path(public, path, normalized)
            changed = True

    return public, plaintext or None, changed


def missing_required_fields(spec: ProviderSpec, public_config: Any, *, configured_secrets: bool) -> list[str]:
    """Which required fields are still unset, as dotted paths.

    Replaces a hand-written "App ID and App Secret are required" check that
    named one provider's fields. Secret fields cannot be inspected -- they live
    encrypted -- so the caller passes whether *any* secret is stored, which is
    the same granularity the secret store offers.
    """

    missing: list[str] = []
    for field in spec.form.fields:
        if not field.required:
            continue
        if field.secret:
            if not configured_secrets:
                missing.append(field.path)
            continue
        value = _read_path(public_config, field.path)
        if value is None or value == "" or value == []:
            missing.append(field.path)
    return missing
