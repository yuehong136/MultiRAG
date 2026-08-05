"""Contracts the provider spec layer has to keep for the registry to be trustworthy."""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import SecretStr

from api.channel_providers import (
    UnknownChannelProvider,
    is_registered,
    provider_names,
    provider_spec,
    provider_specs,
    resolve_path,
    transport_module,
)
from api.channel_providers.spec import ProviderSpec


def test_importing_specs_stays_pure() -> None:
    """No ORM, no web framework, no transport SDK.

    Both the control plane and the worker import this package, and each refuses
    the other's dependencies. import-linter covers the first-party half of that
    rule; it cannot express "no third-party SDK", so this runs in a subprocess
    and looks at what actually landed in ``sys.modules``.

    lark_oapi matters most: it installs a process-global event loop on import,
    which is exactly what the API process must not acquire just to answer
    "which providers exist".
    """

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            ("import sys; import api.channel_providers as p; p.provider_specs(); banned = {'lark_oapi', 'sqlalchemy', 'fastapi', 'redis'} & set(sys.modules); print(sorted(banned))"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"spec import pulled in {result.stdout.strip()}"


def test_registry_fails_closed_on_unknown_names() -> None:
    with pytest.raises(UnknownChannelProvider):
        provider_spec("nope")
    with pytest.raises(UnknownChannelProvider):
        transport_module("nope")
    assert is_registered("nope") is False


def test_every_registered_name_resolves_to_a_matching_spec() -> None:
    """A half-registered provider -- resolvable by one half of the system and
    not the other -- is the failure mode a single registry exists to prevent."""

    names = provider_names()
    assert names == tuple(sorted(names)), "order must be stable for deterministic manifests"
    assert names, "at least one provider must be registered"

    for name in names:
        spec = provider_spec(name)
        assert spec.name == name
        assert is_registered(name)
        # The transport half is referenced by path and imported lazily, so this
        # only asserts the mapping exists -- importing it here would defeat the
        # purity test above.
        assert transport_module(name).startswith("api.channels.")


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_spec_paths_resolve_against_the_provider_config_model(spec: ProviderSpec) -> None:
    """Every declared path must exist in the model it claims to describe.

    These paths drive the secret split and the account-uniqueness check, so a
    typo would silently route a credential into the public config column.
    """

    empty = spec.config_model().model_dump(mode="python")

    for path in {*spec.secret_paths, spec.account_identity_path}:
        parts = path.split(".")
        cursor: object = empty
        for part in parts:
            assert isinstance(cursor, dict), f"{spec.name}: {path} traverses a non-mapping at {part!r}"
            assert part in cursor, f"{spec.name}: {path} is not a field of {spec.config_model.__name__}"
            cursor = cursor[part]


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_declared_secret_paths_match_the_models_secret_fields(spec: ProviderSpec) -> None:
    """`secret_paths` and `SecretStr` must agree.

    They are two statements of the same fact. If a field is a SecretStr but not
    declared secret it would be written to the public config; declared but not
    a SecretStr and it would be dumped in the clear on the way there.
    """

    discovered: set[str] = set()

    def walk(model: type, prefix: str) -> None:
        for field_name, field in model.model_fields.items():
            annotation = field.annotation
            path = f"{prefix}{field_name}"
            if annotation is SecretStr or (hasattr(annotation, "__args__") and SecretStr in annotation.__args__):
                discovered.add(path)
            elif hasattr(annotation, "model_fields"):
                walk(annotation, f"{path}.")

    walk(spec.config_model, "")
    assert discovered == set(spec.secret_paths)


def test_account_identity_reads_the_declared_path() -> None:
    spec = provider_spec("feishu")

    assert spec.account_identity({"credential": {"app_id": "cli_aaaaaaaa"}}) == "cli_aaaaaaaa"
    # Absent, blank and wrong-shaped all mean "no account configured yet"
    # rather than an error: enable-time checks report that, not this.
    assert spec.account_identity({"credential": {}}) is None
    assert spec.account_identity({"credential": {"app_id": ""}}) is None
    assert spec.account_identity({"credential": "not-a-mapping"}) is None
    assert spec.account_identity({}) is None


def test_resolve_path_never_raises_on_malformed_config() -> None:
    assert resolve_path({"a": {"b": 1}}, "a.b") == 1
    assert resolve_path({"a": 1}, "a.b") is None
    assert resolve_path(None, "a") is None
    assert resolve_path([], "a") is None
