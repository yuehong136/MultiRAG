"""Public EIM-A2 JWKS HTTP contract."""

from __future__ import annotations

from api.identity.mcp_issuer.service import McpTokenIssuer


class _FakeIssuer(McpTokenIssuer):
    jwks_cache_ttl_seconds = 300

    def __init__(self) -> None:
        pass

    def jwks_document(self) -> dict[str, object]:
        return {
            "keys": [
                {
                    "alg": "ES256",
                    "crv": "P-256",
                    "kid": "current",
                    "kty": "EC",
                    "use": "sig",
                    "x": "x",
                    "y": "y",
                },
            ],
        }


def test_public_jwks_route_is_root_scoped_unauthenticated_and_cacheable(client, monkeypatch) -> None:
    from api.apps import well_known

    monkeypatch.setattr(well_known, "get_mcp_token_issuer", lambda: _FakeIssuer())

    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.json()["keys"][0]["kid"] == "current"
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v1/.well-known/jwks.json").status_code == 404


def test_public_jwks_route_maps_disabled_or_unready_issuer_to_safe_503(client, monkeypatch) -> None:
    from api.apps import well_known

    def unavailable():
        raise RuntimeError("/private/path/issuer.pem SHOULD-NOT-LEAK")

    monkeypatch.setattr(well_known, "get_mcp_token_issuer", unavailable)

    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 503
    assert response.json() == {"detail": "MCP issuer unavailable"}
    assert "SHOULD-NOT-LEAK" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_unavailable_dependency_returns_no_issuer_without_leaking_failure(monkeypatch, caplog) -> None:
    from api.apps import well_known

    def unavailable():
        raise RuntimeError("secret-marker")

    monkeypatch.setattr(well_known, "get_mcp_token_issuer", unavailable)

    assert well_known.require_mcp_token_issuer() is None
    assert "secret-marker" not in caplog.text


def test_disabled_issuer_is_an_expected_503_without_error_log(monkeypatch, caplog) -> None:
    from api.apps import well_known
    from common.app_config import AppConfigError

    def disabled():
        raise AppConfigError("identity.mcp_issuer is disabled")

    monkeypatch.setattr(well_known, "get_mcp_token_issuer", disabled)

    assert well_known.require_mcp_token_issuer() is None
    assert not caplog.records
