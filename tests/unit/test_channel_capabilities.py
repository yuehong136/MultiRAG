"""Contracts for Provider, target and run-policy capability negotiation."""

from __future__ import annotations

import subprocess
import sys

from api.channel_capabilities import (
    ProviderCapabilities,
    RunCapabilityPolicy,
    TargetCapabilities,
    resolve_effective_reply_capabilities,
)


def _provider(**overrides: bool) -> ProviderCapabilities:
    values = {
        "private_chat": True,
        "group_chat": False,
        "text": True,
        "files": False,
        "images": False,
        "streaming_cards": True,
        "progressive_reply": True,
        "interactive_actions": True,
        "cancel_control": True,
        "feedback_control": True,
        "threaded_reply": True,
    }
    values.update(overrides)
    return ProviderCapabilities(**values)


def _target(**overrides: object) -> TargetCapabilities:
    values: dict[str, object] = {
        "streaming": True,
        "cancellable": True,
        "regeneration": "always",
        "retryable": True,
        "feedback": True,
        "commit_mode": "candidate_cas",
        "effect_class": "generation_only",
    }
    values.update(overrides)
    return TargetCapabilities.model_validate(values)


def test_effective_capabilities_are_a_fieldwise_intersection() -> None:
    resolved = resolve_effective_reply_capabilities(
        _provider(feedback_control=False),
        _target(cancellable=False, regeneration="never"),
        RunCapabilityPolicy(retry=False),
    )

    assert resolved.model_dump() == {
        "progressive_reply": True,
        "cancel_queued": True,
        "cancel_running": False,
        "regenerate": False,
        "retry": False,
        "feedback": False,
    }


def test_noninteractive_provider_cannot_inherit_target_actions() -> None:
    resolved = resolve_effective_reply_capabilities(
        _provider(interactive_actions=False),
        _target(),
        RunCapabilityPolicy(),
    )

    assert resolved.cancel_queued is False
    assert resolved.cancel_running is False
    assert resolved.regenerate is False
    assert resolved.retry is False
    assert resolved.feedback is False
    assert resolved.progressive_reply is True


def test_unresolved_conditional_regeneration_fails_closed() -> None:
    resolved = resolve_effective_reply_capabilities(
        _provider(),
        _target(regeneration="conditional"),
        RunCapabilityPolicy(),
    )

    assert resolved.regenerate is False


def test_malformed_explicit_run_policy_fails_closed_without_affecting_absent_policy() -> None:
    assert RunCapabilityPolicy.from_binding_policy({}) == RunCapabilityPolicy()

    malformed = RunCapabilityPolicy.from_binding_policy({"reply_capabilities": "not-an-object"})
    assert malformed.model_dump() == {
        "progressive_reply": False,
        "cancel": False,
        "regenerate": False,
        "retry": False,
        "feedback": False,
    }

    partial = RunCapabilityPolicy.from_binding_policy({"reply_capabilities": {"cancel": False, "feedback": "invalid"}})
    assert partial.cancel is False
    assert partial.feedback is False
    assert partial.regenerate is True


def test_worker_capability_negotiation_imports_no_database_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            ("import sys; import api.channels.worker; print(sorted(name for name in ('sqlalchemy', 'api.db', 'api.db.db_models') if name in sys.modules))"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
