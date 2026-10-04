from types import SimpleNamespace
from typing import Any

import pytest
from google.oauth2.credentials import Credentials

from common.data_source.google_util import resource


@pytest.mark.parametrize("factory", [resource.get_gmail_service, resource.get_drive_service, resource.get_admin_service, resource.get_google_docs_service])
@pytest.mark.parametrize("delegated", [False, True])
def test_google_discovery_cache_is_disabled_for_both_credential_types(monkeypatch: pytest.MonkeyPatch, factory: Any, delegated: bool) -> None:
    calls: list[dict[str, Any]] = []
    service = SimpleNamespace()

    class ServiceAccount:
        def with_subject(self, subject: str) -> "ServiceAccount":
            assert subject == "user@example.test"
            return self

    monkeypatch.setattr(resource, "ServiceAccountCredentials", ServiceAccount)
    monkeypatch.setattr(resource, "build", lambda *args, **kwargs: calls.append(kwargs) or service)
    creds = ServiceAccount() if delegated else Credentials("synthetic")
    assert factory(creds, "user@example.test") is service
    assert calls == [{"credentials": creds, "cache_discovery": False}]
