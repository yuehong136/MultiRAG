"""EIM-I1 external-only user model and password-boundary contracts."""

from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db import UserAccountKind
from api.db.db_models import User
from api.db.services.user_service import UserService


def _external_user(*, email: str | None = None) -> User:
    return User(
        id="external-user",
        nickname="External User",
        email=email,
        password=None,
        account_kind=UserAccountKind.EXTERNAL.value,
    )


def test_external_user_serializes_null_email_and_account_kind() -> None:
    payload = _external_user().to_dict()

    assert payload["email"] is None
    assert payload["account_kind"] == UserAccountKind.EXTERNAL.value
    assert "password" not in payload
    assert "access_token" not in payload


def test_create_new_user_returns_only_public_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.db.joint_services import user_account_service

    db = Mock(spec=Session)
    persisted_user = User(
        id="created-user",
        nickname="Created User",
        email="created@example.test",
        password="stored-password-hash",
        access_token="stored-access-token",
        account_kind=UserAccountKind.LOCAL.value,
    )
    monkeypatch.setattr(
        user_account_service.UserService,
        "save",
        classmethod(lambda cls, db, **kwargs: persisted_user),
    )
    monkeypatch.setattr(user_account_service, "get_init_tenant_llm", lambda db, user_id: [])
    monkeypatch.setattr(
        user_account_service,
        "ensure_tenant_model_id_for_params",
        lambda db, user_id, tenant: tenant,
    )
    for service, method in (
        (user_account_service.TenantService, "insert"),
        (user_account_service.TenantService, "update_by_id"),
        (user_account_service.UserTenantService, "insert"),
        (user_account_service.TenantLLMService, "insert_many"),
        (user_account_service.FileService, "insert"),
    ):
        monkeypatch.setattr(service, method, Mock(return_value=True))

    result = user_account_service.create_new_user(
        db,
        {
            "email": "created@example.test",
            "nickname": "Created User",
            "password": "plaintext-password",
            "login_channel": "password",
        },
    )

    assert result["success"] is True
    assert result["user_info"]["id"] == "created-user"
    assert "password" not in result["user_info"]
    assert "access_token" not in result["user_info"]


def test_password_verification_fails_closed_for_absent_or_malformed_hash() -> None:
    assert UserService.verify_password("secret", None) is False
    assert UserService.verify_password("secret", "not-a-bcrypt-hash") is False


def test_save_external_user_preserves_null_password(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Mock(spec=Session)
    hash_password = Mock(side_effect=AssertionError("null password must not be hashed"))
    monkeypatch.setattr(UserService, "hash_password", hash_password)

    user = UserService.save(
        db,
        id="external-user",
        nickname="External User",
        email=None,
        password=None,
        account_kind=UserAccountKind.EXTERNAL.value,
    )

    assert user.password is None
    assert user.email is None
    assert user.account_kind == UserAccountKind.EXTERNAL.value
    hash_password.assert_not_called()
    db.add.assert_called_once_with(user)
    db.commit.assert_called_once_with()


def test_save_rejects_local_password_on_external_user() -> None:
    db = Mock(spec=Session)

    with pytest.raises(ValueError, match="External-only users cannot have a local password"):
        UserService.save(
            db,
            nickname="External User",
            email=None,
            password="must-not-be-accepted",
            account_kind=UserAccountKind.EXTERNAL.value,
        )

    db.add.assert_not_called()


def test_query_user_handles_identity_only_record_without_bcrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_user = _external_user(email="external@example.test")
    monkeypatch.setattr(
        UserService,
        "query_password_user_by_email",
        classmethod(lambda cls, db, email: external_user),
    )

    assert UserService.query_user(Mock(spec=Session), "external@example.test", "secret") is None


def test_missing_external_inviter_email_uses_safe_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.apps.services import tenant_api_service

    external_user = _external_user()
    external_user.nickname = ""
    monkeypatch.setattr(
        UserService,
        "get_by_id",
        classmethod(lambda cls, db, user_id: external_user),
    )

    assert tenant_api_service.inviter_display_name(Mock(spec=Session), "external-user", None) == "MultiRAG user"
