"""Contracts the provider spec layer has to keep for the registry to be trustworthy."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys

import pytest
from pydantic import SecretStr

from api.channel_providers import (
    UnknownChannelProvider,
    is_registered,
    provider_names,
    provider_spec,
    resolve_path,
    transport_module,
)
from api.channel_providers.spec import ProviderSpec

# STATUS_DLL_INIT_FAILED. Windows raises it when a process cannot be created at
# all, typically under resource pressure late in a long run.
_WINDOWS_DLL_INIT_FAILED = 0xC0000142


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

    if result.returncode == _WINDOWS_DLL_INIT_FAILED and not result.stdout.strip():
        # The interpreter never started, so this check produced no signal at
        # all -- calling that a pass would be a lie and calling it a failure
        # would blame the code under test. Seen on Windows late in a full-suite
        # run, same return code as the template-render tests that fail there
        # for the same reason; it never reproduces in isolation or on CI Linux.
        pytest.skip(f"subprocess could not start (rc={result.returncode}); purity unverified")

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


def _declared_specs() -> tuple[ProviderSpec, ...]:
    """Every spec module in the package, registered or not.

    The consistency checks below used to run over ``provider_specs()``, which
    is the *registered* set -- so a spec written but not yet wired in got no
    coverage at all, and the first thing that would exercise it was
    registration itself. Registration is exactly when a broken spec is most
    expensive: it becomes reachable from the API in the same commit.
    """

    package = importlib.import_module("api.channel_providers")
    found: list[ProviderSpec] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"api.channel_providers.{module_info.name}")
        candidate = getattr(module, "PROVIDER_SPEC", None)
        if isinstance(candidate, ProviderSpec):
            found.append(candidate)
    assert found, "no provider specs discovered"
    return tuple(sorted(found, key=lambda spec: spec.name))


def test_every_registered_provider_has_a_declared_spec() -> None:
    declared = {spec.name for spec in _declared_specs()}
    assert set(provider_names()) <= declared


# Mirrors `ChannelFieldKind` in `web:src/api/channel.ts`. A provider using a
# kind outside this set needs a frontend release before its form is usable,
# which is the one thing the FieldSpec contract exists to avoid -- so a new
# entry here is a deliberate cross-repo decision, not an implementation detail.
_CLIENT_RENDERED_KINDS = frozenset({"text", "password", "string_list", "select", "switch"})


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
def test_a_provider_form_needs_no_widget_the_client_lacks(spec: ProviderSpec) -> None:
    """The acceptance criterion for the provider work, in test form (CHN-P10).

    A second provider must be renderable by a frontend build made before it
    existed. That holds exactly while its fields use kinds the client already
    has -- an unknown kind renders as a disabled input, which is a safe
    degradation but not a usable form.
    """

    unknown = sorted({field.kind for field in spec.form.fields} - _CLIENT_RENDERED_KINDS)
    assert not unknown, f"{spec.name} needs a frontend release for: {', '.join(unknown)}"


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
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


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
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


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
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


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
def test_form_secret_flags_agree_with_the_declared_secret_paths(spec: ProviderSpec) -> None:
    """`secret=True` and `secret_paths` are the same fact stated twice.

    A field marked secret but not routed to the secret store would be written
    into the public config column; routed but not marked and the form would
    render it as a plain text input with the value echoed back.
    """

    marked = {field.path for field in spec.form.fields if field.secret}
    assert marked == set(spec.secret_paths)


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
def test_select_fields_carry_options_and_a_valid_default(spec: ProviderSpec) -> None:
    for field in spec.form.fields:
        if field.kind == "select":
            assert field.options, f"{spec.name}: select field {field.path} has no options"
            values = {option.value for option in field.options}
            if field.default is not None:
                assert field.default in values, f"{spec.name}: {field.path} default is not one of its options"
        else:
            assert field.options is None, f"{spec.name}: {field.path} is {field.kind} but carries options"


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
def test_secret_paths_are_a_subset_of_the_credential(spec: ProviderSpec) -> None:
    """A secret that is not part of the credential could never be reassembled.

    The worker receives one credential built from ``credential_paths``; a
    secret outside that set would be encrypted, stored, and then never handed
    to anyone.
    """

    assert spec.secret_paths <= spec.credential_paths


@pytest.mark.parametrize("spec", _declared_specs(), ids=lambda spec: spec.name)
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


def test_the_payload_the_client_assembles_is_the_one_the_provider_accepts() -> None:
    """The backend half of the cross-repo acceptance (CHN-X3).

    The literal below is copied from `assembleConfig`'s asserted output in
    `web:src/api/__tests__/channel.test.ts`, where a frontend build that
    predates DingTalk turns the server's field list into this shape. If either
    side drifts, one of the two tests goes red -- which is the only mechanism
    binding them, since the repos have separate CI and no shared fixture.
    """

    from api.channel_providers.dingtalk import PROVIDER_SPEC as dingtalk
    from api.channel_providers.functions import split_config

    assembled = {
        "credential": {
            "client_id": "dingaaaaaaaaaaaaaaaa",
            "client_secret": "secret-aaaa-bbbb-cccc",
        },
        "robot_code": "robot-aaaa",
        "allowed_user_ids": ["user_a", "user_b"],
    }

    parsed = dingtalk.config_model.model_validate(assembled)
    public, plaintext = split_config(dingtalk, parsed)

    # The secret half is routed to the encrypted store and the public half to
    # the column that read paths echo -- keyed by leaf name, so this provider
    # needed no control-plane change to get its own split.
    assert plaintext == {"client_secret": "secret-aaaa-bbbb-cccc"}
    assert public == {
        "credential": {"client_id": "dingaaaaaaaaaaaaaaaa"},
        "robot_code": "robot-aaaa",
        "allowed_user_ids": ["user_a", "user_b"],
    }
    assert "secret-aaaa-bbbb-cccc" not in str(public)
