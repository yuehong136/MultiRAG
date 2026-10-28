"""Real PyJWT enforces discovery-pinned asymmetric algorithms and claims."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.apps.auth import oidc


def _client(monkeypatch: pytest.MonkeyPatch, advertised: Any = None) -> oidc.OIDCClient:
    metadata = {
        "issuer": "https://issuer.example/",
        "jwks_uri": "https://issuer.example/jwks",
        "authorization_endpoint": "https://issuer.example/auth",
        "token_endpoint": "https://issuer.example/token",
        "userinfo_endpoint": "https://issuer.example/info",
        "id_token_signing_alg_values_supported": advertised,
    }
    monkeypatch.setattr(oidc.OIDCClient, "_load_oidc_metadata", staticmethod(lambda _: metadata))
    return oidc.OIDCClient({"issuer": "https://issuer.example/", "client_id": "client", "client_secret": "fixture", "redirect_uri": "https://app.example/callback"})


@pytest.mark.parametrize(
    "advertised,expected",
    [
        (None, ["RS256"]),
        ([], ["RS256"]),
        ("ES256", ["RS256"]),
        ({"RS256": True}, ["RS256"]),
        (["HS256", "none", "bogus"], ["RS256"]),
        (["RS256", None, 42, {}, "ES384", "RS256"], ["RS256", "ES384"]),
        (["PS256", "EdDSA"], ["PS256", "EdDSA"]),
    ],
)
def test_discovery_policy(monkeypatch: pytest.MonkeyPatch, advertised: Any, expected: list[str]) -> None:
    assert _client(monkeypatch, advertised).id_token_signing_algs == expected


@pytest.mark.parametrize("issuer", ["https://issuer.example/app", "https://issuer.example/app/", "https://issuer.example/app///"])
def test_discovery_trims_only_url_join(monkeypatch: pytest.MonkeyPatch, issuer: str) -> None:
    request = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"issuer": issuer}))
    monkeypatch.setattr(oidc, "sync_request", request)
    assert oidc.OIDCClient._load_oidc_metadata(issuer) == {"issuer": issuer}
    request.assert_called_once_with("GET", "https://issuer.example/app/.well-known/openid-configuration", timeout=7)


def _key_lookup(monkeypatch: pytest.MonkeyPatch, key: Any) -> None:
    monkeypatch.setattr(oidc.jwt, "PyJWKClient", lambda _: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key)))


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_forged_symmetric_or_unsigned_token_rejected(monkeypatch: pytest.MonkeyPatch, algorithm: str) -> None:
    client = _client(monkeypatch, ["RS256", "HS256", "none"])
    secret = b"public-key-der-like-non-pem-fixture-0123456789"
    token = jwt.encode({"sub": "victim", "iss": client.issuer, "aud": "client"}, secret if algorithm == "HS256" else "", algorithm=algorithm)
    _key_lookup(monkeypatch, secret)
    with pytest.raises(ValueError, match="specified alg value is not allowed"):
        client.parse_id_token(token)


@pytest.mark.parametrize("fault", [None, "issuer", "audience", "signature", "expired"])
def test_real_rsa_signature_and_claim_checks(monkeypatch: pytest.MonkeyPatch, fault: str | None) -> None:
    client = _client(monkeypatch)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "sub": "subject",
        "iss": "other" if fault == "issuer" else client.issuer,
        "aud": "other" if fault == "audience" else "client",
        "exp": datetime.now(UTC) + timedelta(seconds=-60 if fault == "expired" else 60),
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    verify = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key() if fault == "signature" else key.public_key()
    _key_lookup(monkeypatch, verify)
    if fault:
        with pytest.raises(ValueError, match="Error parsing ID Token"):
            client.parse_id_token(token)
    else:
        assert client.parse_id_token(token)["sub"] == "subject"
