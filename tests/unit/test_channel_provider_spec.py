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

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert result.stdout.strip() == "[]", f"spec import pulled in {result.stdout.strip()}\nstderr:\n{result.stderr}"


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


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_form_fields_describe_real_config_paths(spec: ProviderSpec) -> None:
    """The render contract and the validation contract must not drift apart.

    ``form`` and ``config_schema`` are two derived views of one spec. If a form
    field names a path the model does not accept, the client renders an input
    whose value the server will reject with ``extra="forbid"`` -- and the admin
    gets a validation error about a field the page told them to fill in.
    """

    empty = spec.config_model().model_dump(mode="python")

    for field in spec.form.fields:
        cursor: object = empty
        for part in field.path.split("."):
            assert isinstance(cursor, dict), f"{spec.name}: {field.path} traverses a non-mapping"
            assert part in cursor, f"{spec.name}: form field {field.path} is not accepted by {spec.config_model.__name__}"
            cursor = cursor[part]


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_form_secret_flags_agree_with_the_declared_secret_paths(spec: ProviderSpec) -> None:
    """`secret=True` and `secret_paths` are the same fact stated twice.

    A field marked secret but not routed to the secret store would be written
    into the public config column; routed but not marked and the form would
    render it as a plain text input with the value echoed back.
    """

    marked = {field.path for field in spec.form.fields if field.secret}
    assert marked == set(spec.secret_paths)


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_select_fields_carry_options_and_a_valid_default(spec: ProviderSpec) -> None:
    for field in spec.form.fields:
        if field.kind == "select":
            assert field.options, f"{spec.name}: select field {field.path} has no options"
            values = {option.value for option in field.options}
            if field.default is not None:
                assert field.default in values, f"{spec.name}: {field.path} default is not one of its options"
        else:
            assert field.options is None, f"{spec.name}: {field.path} is {field.kind} but carries options"


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_secret_paths_are_a_subset_of_the_credential(spec: ProviderSpec) -> None:
    """A secret that is not part of the credential could never be reassembled.

    The worker receives one credential built from ``credential_paths``; a
    secret outside that set would be encrypted, stored, and then never handed
    to anyone.
    """

    assert spec.secret_paths <= spec.credential_paths


@pytest.mark.parametrize("spec", provider_specs(), ids=lambda spec: spec.name)
def test_form_field_paths_are_unique(spec: ProviderSpec) -> None:
    """Two fields on one path would silently overwrite each other on submit."""

    paths = [field.path for field in spec.form.fields]
    assert len(paths) == len(set(paths))


def test_manifest_exposes_the_form_alongside_the_schema() -> None:
    """The wire shape the frontend consumes (CHN-P5 onward)."""

    from api.channel_control.schemas import provider_manifests

    manifest = next(item for item in provider_manifests() if item.provider == "feishu")
    payload = manifest.model_dump(mode="json")

    assert payload["form"]["version"] == 1
    assert [field["path"] for field in payload["form"]["fields"]] == [
        "credential.app_id",
        "credential.app_secret",
        "domain",
        "allowed_open_ids",
    ]
    # required lives in the form, not the schema: every config field carries a
    # default so PATCH can mean merge, which erases the schema's required array.
    assert payload["config_schema"].get("required") is None
    assert [field["path"] for field in payload["form"]["fields"] if field["required"]] == [
        "credential.app_id",
        "credential.app_secret",
        "domain",
    ]
    secret_fields = [field for field in payload["form"]["fields"] if field["secret"]]
    assert [field["path"] for field in secret_fields] == ["credential.app_secret"]
    assert secret_fields[0]["kind"] == "password"


def test_resolve_path_never_raises_on_malformed_config() -> None:
    assert resolve_path({"a": {"b": 1}}, "a.b") == 1
    assert resolve_path({"a": 1}, "a.b") is None
    assert resolve_path(None, "a") is None
    assert resolve_path([], "a") is None
