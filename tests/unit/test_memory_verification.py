"""Strict human-check command and cursor boundaries, without external services."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.memory_verification_api import VerificationRequest
from memory.revisions.domain import RevisionValidationError
from memory.verification.domain import create_request, cursor_decode, cursor_encode, head_version


def command(**changes: Any) -> dict[str, Any]:
    return {
        "expected_head_version": "0",
        "expected_binding_sha256": "a" * 64,
        "result": "human_confirmed",
        "method": "source_comparison",
        "applicability": "仅适用这条记录及其已展示的来源版本。",
        "notes": "人工核对记录。",
        "evidence": [{"description": "逐项与来源比较。", "reference": "source:10"}],
        "checked_at": None,
        **changes,
    }


@pytest.mark.parametrize("value", [0, "0", 2**63 - 1, str(2**63 - 1)])
def test_exact_version_preserves_zero_and_int64(value: Any) -> None:
    assert head_version(value) == int(value)


@pytest.mark.parametrize("value", [True, False, -1, "01", "-1", "1.0", 1.0, 2**63, " 1", None])
def test_ambiguous_versions_are_rejected(value: Any) -> None:
    with pytest.raises(RevisionValidationError):
        head_version(value)


@pytest.mark.parametrize(
    "changes",
    [{"actor_id": "forged"}, {"assessment": {"verification_status": "verified"}}, {"result": "verified"}, {"expected_head_version": True}, {"evidence": [{"description": "check", "trusted": True}]}],
)
def test_public_schema_rejects_authority_and_wrong_types(changes: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, RevisionValidationError)):
        VerificationRequest.model_validate({"operation_id": "human-check", **command(), **changes})


def test_reported_time_is_optional_and_never_an_implicit_trusted_clock() -> None:
    reported = datetime.now(UTC) - timedelta(minutes=2)
    assert create_request(**command(checked_at=reported))["checked_at"] == reported.isoformat()
    assert create_request(**command())["checked_at"] is None
    for value in (datetime.now(), datetime.now(UTC) + timedelta(days=1)):
        with pytest.raises(RevisionValidationError, match="invalid_reported_verification_time"):
            create_request(**command(checked_at=value))


def test_cursor_binds_actor_bank_filter_and_limit_without_executing_text() -> None:
    value = cursor_encode("a" * 64, "f" * 32)
    assert cursor_decode(value, "a" * 64) == "f" * 32
    for invalid in (value, "!bad!", "a" * 513):
        with pytest.raises(RevisionValidationError, match="invalid_verification_cursor"):
            cursor_decode(invalid, "b" * 64)
