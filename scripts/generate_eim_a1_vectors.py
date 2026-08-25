# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "ecdsa==0.19.1",
# ]
# ///
"""Generate the deterministic EIM-A1 JWT/JWKS interoperability corpus.

The signing keys are deliberately tiny, public, test-only secret exponents.
They are derived in memory and are never serialized as private JWK/PEM files.
RFC 6979 signing from ``ecdsa`` makes every compact ES256 token byte-stable.

Run with:

    uv run --script scripts/generate_eim_a1_vectors.py

Normal unit tests never invoke this generator or re-sign golden tokens. Corpus
changes are deliberate contract revisions and must be copied byte-for-byte to
``of_mcp`` before either repository records EIM-A1 complete.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_string

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "tests" / "fixtures" / "eim_a1" / "v2"

CONTRACT_VERSION = "eim-a1/v2"
VALIDATION_TIME = 1_786_492_800  # 2026-08-12T00:00:00Z
CLOCK_SKEW_SECONDS = 30
MAX_TOKEN_BYTES = 4096

ACCESS_ISSUER = "https://auth.multirag.example"
INTERNAL_ISSUER = "https://gateway.ofmcp.example"
GATEWAY_RESOURCE = "https://gateway.ofmcp.example/mcp"
INBOUND_RESOURCE = "https://multirag.example/mcp"
LEAVE_PROXY_RESOURCE = "https://leave.ofmcp.example/internal/mcp"
MEDIC_PROXY_RESOURCE = "https://medic.ofmcp.example/internal/mcp"

ACCESS_CURRENT_KID = "test-only-eim-a1-access-current"
ACCESS_OLD_KID = "test-only-eim-a1-access-old"
INTERNAL_KID = "test-only-eim-a1-internal-current"

# Public, low-entropy fixture values. Never use these exponents for any other
# purpose. They are intentionally visible in source so the corpus is reproducible.
ACCESS_CURRENT_KEY = SigningKey.from_secret_exponent(11, curve=NIST256p, hashfunc=hashlib.sha256)
ACCESS_OLD_KEY = SigningKey.from_secret_exponent(12, curve=NIST256p, hashfunc=hashlib.sha256)
INTERNAL_KEY = SigningKey.from_secret_exponent(21, curve=NIST256p, hashfunc=hashlib.sha256)

PLATFORM_USER_ID = "11111111111111111111111111111111"
TENANT_ID = "tenant-test-a"
AGENT_ID = "agent-test-a"
PROVIDER_IDENTITY = {
    "provider": "feishu",
    "provider_tenant": "provider-tenant-test-a",
    "subject": "provider-user-test-0001",
    "subject_type": "user_id",
}

JsonObject = dict[str, Any]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _public_jwk(key: SigningKey, kid: str) -> JsonObject:
    point = key.verifying_key.to_string()
    return {
        "alg": "ES256",
        "crv": "P-256",
        "kid": kid,
        "kty": "EC",
        "use": "sig",
        "x": _b64url(point[:32]),
        "y": _b64url(point[32:]),
    }


def _sign_raw(header_json: bytes, payload_json: bytes, key: SigningKey) -> str:
    encoded_header = _b64url(header_json)
    encoded_payload = _b64url(payload_json)
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = key.sign_deterministic(
        signing_input,
        hashfunc=hashlib.sha256,
        sigencode=sigencode_string,
    )
    return f"{encoded_header}.{encoded_payload}.{_b64url(signature)}"


def _sign(header: JsonObject, payload: JsonObject, key: SigningKey) -> str:
    return _sign_raw(_canonical_json(header), _canonical_json(payload), key)


def _unsigned_none_token(payload: JsonObject) -> str:
    header = {"alg": "none", "kid": ACCESS_CURRENT_KID, "typ": "at+jwt"}
    return f"{_b64url(_canonical_json(header))}.{_b64url(_canonical_json(payload))}."


def _hs256_token(payload: JsonObject) -> str:
    header = {"alg": "HS256", "kid": ACCESS_CURRENT_KID, "typ": "at+jwt"}
    encoded_header = _b64url(_canonical_json(header))
    encoded_payload = _b64url(_canonical_json(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(b"test-only-eim-a1-hmac-confusion", signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url(signature)}"


def _access_claims(**overrides: Any) -> JsonObject:
    claims: JsonObject = {
        "agent_id": AGENT_ID,
        "aud": GATEWAY_RESOURCE,
        "client_id": "multirag-test-client",
        "exp": VALIDATION_TIME + 240,
        "iat": VALIDATION_TIME - 60,
        "iss": ACCESS_ISSUER,
        "jti": "access-jti-test-00000001",
        "nbf": VALIDATION_TIME - 60,
        "scope": "leave:read",
        "sub": PLATFORM_USER_ID,
        "tenant_id": TENANT_ID,
        "token_use": "mcp_access",
    }
    claims.update(overrides)
    return claims


def _internal_claims(*, parent_jti: str, **overrides: Any) -> JsonObject:
    claims: JsonObject = {
        "act": {"sub": "ofmcp-gateway-test-workload"},
        "agent_id": AGENT_ID,
        "aud": LEAVE_PROXY_RESOURCE,
        "client_id": "ofmcp-gateway-test-client",
        "exp": VALIDATION_TIME + 55,
        "iat": VALIDATION_TIME - 5,
        "iss": INTERNAL_ISSUER,
        "jti": "actor-jti-test-00000001",
        "nbf": VALIDATION_TIME - 5,
        "parent_jti_hash": _b64url(hashlib.sha256(parent_jti.encode("utf-8")).digest()),
        "scope": "leave:read",
        "sub": PLATFORM_USER_ID,
        "tenant_id": TENANT_ID,
        "token_use": "mcp_internal_actor",
        "trace_id": "trace-test-0000000000000001",
    }
    claims.update(overrides)
    return claims


def _header(*, kid: str = ACCESS_CURRENT_KID, **overrides: Any) -> JsonObject:
    value: JsonObject = {"alg": "ES256", "kid": kid, "typ": "at+jwt"}
    value.update(overrides)
    return value


def _normalize_claims(claims: JsonObject) -> JsonObject:
    normalized = copy.deepcopy(claims)
    scope = normalized.pop("scope")
    normalized["scopes"] = sorted(scope.split(" "))
    return normalized


def _expected(
    *,
    authentication: str = "accept",
    authorization: str = "allow",
    failure_reason: str | None = None,
    normalized_claims: JsonObject | None = None,
    oauth_error: str | None = None,
) -> JsonObject:
    if authentication == "reject":
        return {
            "authentication": "reject",
            "authorization": "not_evaluated",
            "failure_reason": failure_reason,
            "http_status": 401,
            "normalized_claims": None,
            "oauth_error": oauth_error or "invalid_token",
        }
    if authorization == "deny":
        return {
            "authentication": "accept",
            "authorization": "deny",
            "failure_reason": failure_reason,
            "http_status": 403,
            "normalized_claims": normalized_claims,
            "oauth_error": oauth_error,
        }
    return {
        "authentication": "accept",
        "authorization": "allow",
        "failure_reason": None,
        "http_status": 200,
        "normalized_claims": normalized_claims,
        "oauth_error": None,
    }


def _manifest_schema() -> JsonObject:
    string_array = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
    normalized_claims = {"type": ["object", "null"]}
    expected = {
        "type": "object",
        "additionalProperties": False,
        "required": ["authentication", "authorization", "http_status", "oauth_error", "failure_reason", "normalized_claims"],
        "properties": {
            "authentication": {"enum": ["accept", "reject"]},
            "authorization": {"enum": ["allow", "deny", "not_evaluated"]},
            "failure_reason": {"type": ["string", "null"]},
            "http_status": {"enum": [200, 401, 403]},
            "normalized_claims": normalized_claims,
            "oauth_error": {"enum": [None, "invalid_token", "insufficient_scope"]},
        },
    }
    request_context = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "expected_resource",
            "expected_tenant",
            "required_enterprise_subject_type",
            "required_scopes",
            "requires_enterprise_subject",
        ],
        "properties": {
            "expected_resource": {"type": "string", "minLength": 1},
            "expected_tenant": {"type": "string", "minLength": 1},
            "required_enterprise_subject_type": {"type": ["string", "null"]},
            "required_scopes": string_array,
            "requires_enterprise_subject": {"type": "boolean"},
        },
    }
    token_case = {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected", "expected_profile", "id", "jwks_file", "request_context", "token_file"],
        "properties": {
            "expected": expected,
            "expected_profile": {"enum": ["mcp_access", "mcp_internal_actor"]},
            "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
            "jwks_file": {"type": "string", "pattern": "^jwks/.+\\.json$"},
            "request_context": request_context,
            "token_file": {"type": "string", "pattern": "^tokens/.+\\.jwt$"},
        },
    }
    issuance_case = {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected", "id", "request"],
        "properties": {
            "expected": {
                "type": "object",
                "additionalProperties": False,
                "required": ["failure_reason", "issuance"],
                "properties": {
                    "failure_reason": {"type": ["string", "null"]},
                    "issuance": {"enum": ["allow", "reject"]},
                },
            },
            "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
            "request": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "allowed_scopes",
                    "assurance_verified",
                    "registered_scopes",
                    "requested_claims",
                    "requested_scopes",
                    "requires_assurance",
                    "subject_source",
                    "tenant_source",
                ],
                "properties": {
                    "allowed_scopes": string_array,
                    "assurance_verified": {"type": "boolean"},
                    "registered_scopes": string_array,
                    "requested_claims": string_array,
                    "requested_scopes": string_array,
                    "requires_assurance": {"type": "boolean"},
                    "subject_source": {"enum": ["platform_principal", "provider_external_identifier"]},
                    "tenant_source": {"enum": ["caller_payload", "server_context"]},
                },
            },
        },
    }
    delegation_case = {
        "type": "object",
        "additionalProperties": False,
        "required": ["actor_case_id", "expected", "id", "parent_case_id", "required_preserved_claims"],
        "properties": {
            "actor_case_id": {"type": "string", "minLength": 1},
            "expected": {
                "type": "object",
                "additionalProperties": False,
                "required": ["delegation", "failure_reason"],
                "properties": {
                    "delegation": {"enum": ["allow", "reject"]},
                    "failure_reason": {"type": ["string", "null"]},
                },
            },
            "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
            "parent_case_id": {"type": "string", "minLength": 1},
            "required_preserved_claims": string_array,
        },
    }
    return {
        "$id": "https://schemas.multirag.example/eim-a1/v2/manifest.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "EIM-A1 deterministic token interoperability corpus",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "cases",
            "clock_skew_seconds",
            "contract_version",
            "delegation_cases",
            "issuance_policy_cases",
            "max_token_bytes",
            "profiles",
            "resources",
            "scope_registry",
            "validation_time",
        ],
        "properties": {
            "cases": {"type": "array", "items": token_case, "minItems": 1},
            "clock_skew_seconds": {"type": "integer", "const": CLOCK_SKEW_SECONDS},
            "contract_version": {"const": CONTRACT_VERSION},
            "delegation_cases": {"type": "array", "items": delegation_case, "minItems": 1},
            "issuance_policy_cases": {"type": "array", "items": issuance_case, "minItems": 1},
            "max_token_bytes": {"type": "integer", "const": MAX_TOKEN_BYTES},
            "profiles": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mcp_access", "mcp_internal_actor"],
                "properties": {
                    "mcp_access": {"$ref": "#/$defs/profile"},
                    "mcp_internal_actor": {"$ref": "#/$defs/profile"},
                },
            },
            "resources": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["profile", "uri"],
                    "properties": {
                        "profile": {"enum": ["mcp_access", "mcp_internal_actor"]},
                        "uri": {"type": "string", "pattern": "^https://"},
                    },
                },
            },
            "scope_registry": {"type": "object", "minProperties": 1, "additionalProperties": string_array},
            "validation_time": {"type": "integer", "const": VALIDATION_TIME},
        },
        "$defs": {
            "profile": {
                "type": "object",
                "additionalProperties": False,
                "required": ["allowed_acr_values", "allowed_amr_values", "allowed_claims", "issuer", "max_ttl_seconds", "required_claims", "token_use"],
                "properties": {
                    "allowed_acr_values": string_array,
                    "allowed_amr_values": string_array,
                    "allowed_claims": string_array,
                    "issuer": {"type": "string", "pattern": "^https://"},
                    "max_ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                    "required_claims": string_array,
                    "token_use": {"enum": ["mcp_access", "mcp_internal_actor"]},
                },
            }
        },
    }


def _readme() -> str:
    return """# EIM-A1 deterministic JWT/JWKS corpus

This directory is the language-neutral `eim-a1/v2` contract shared byte-for-byte by
MultiRAG and `of_mcp`. Its canonical JWKS files contain only public test keys, and all
identifiers are synthetic `.example` values. The deliberately invalid
`jwks/invalid/private_material.json` contains only a non-key sentinel `d` member that
strict consumers must reject. No production secret, provider credential, user
identifier, or message content belongs here.

The two JWT profiles are private, RFC 9068-shaped project profiles. MCP itself does
not require JWT or ES256. `mcp_access` is resource-bound to a gateway or inbound MCP
resource. `mcp_internal_actor` is issued by the gateway for one exact proxy resource
and carries RFC 8693-style `act` semantics. They use distinct issuer/keyset/resource
boundaries and are never interchangeable.

`cases` are Resource Server authentication/authorization observations.
`issuance_policy_cases` are signer-side rules that a Resource Server cannot infer
from an opaque `sub` or requested scope. `delegation_cases` compare a verified parent
and actor token; a downstream service cannot prove attenuation from the actor token
alone.

`validation_time` is an integer NumericDate. Consumers must disable library wall-clock
checks and apply the manifest time, skew, and profile TTL rules explicitly. Public
OAuth errors stay coarse; `failure_reason` is a stable internal test category and must
not be exposed as a bearer-token oracle.

Normal tests verify the checked-in bytes and never re-sign them. To deliberately
regenerate a new corpus revision:

```bash
uv run --script scripts/generate_eim_a1_vectors.py
```

The generator derives documented test-only P-256 keys in memory and uses RFC 6979
deterministic ECDSA. Copy this entire directory to `of_mcp`, compare the SHA-256 of
`SHA256SUMS`, and run both repositories' independent conformance tests.
"""


def _build_corpus() -> tuple[JsonObject, dict[str, str], dict[str, JsonObject]]:
    token_files: dict[str, str] = {}
    cases: list[JsonObject] = []
    issuance_policy_cases: list[JsonObject] = []
    delegation_cases: list[JsonObject] = []

    access_current_jwk = _public_jwk(ACCESS_CURRENT_KEY, ACCESS_CURRENT_KID)
    access_old_jwk = _public_jwk(ACCESS_OLD_KEY, ACCESS_OLD_KID)
    internal_jwk = _public_jwk(INTERNAL_KEY, INTERNAL_KID)

    jwks_files: dict[str, JsonObject] = {
        "jwks/access.json": {"keys": [access_current_jwk, access_old_jwk]},
        "jwks/internal_actor.json": {"keys": [internal_jwk]},
        "jwks/invalid/duplicate_kid.json": {
            "keys": [access_current_jwk, {**access_old_jwk, "kid": ACCESS_CURRENT_KID}],
        },
        "jwks/invalid/private_material.json": {
            "keys": [{**access_current_jwk, "d": "TEST_ONLY_PRIVATE_MATERIAL_MUST_BE_REJECTED"}],
        },
        "jwks/invalid/wrong_alg.json": {"keys": [{**access_current_jwk, "alg": "ES384"}]},
        "jwks/invalid/wrong_crv.json": {"keys": [{**access_current_jwk, "crv": "P-384"}]},
        "jwks/invalid/wrong_kty.json": {"keys": [{**access_current_jwk, "kty": "RSA"}]},
        "jwks/invalid/wrong_use.json": {"keys": [{**access_current_jwk, "use": "enc"}]},
    }

    def add_case(
        case_id: str,
        token: str,
        *,
        claims: JsonObject | None = None,
        expected_profile: str = "mcp_access",
        jwks_file: str = "jwks/access.json",
        expected_resource: str = "ofmcp_gateway",
        expected_tenant: str = TENANT_ID,
        required_scopes: tuple[str, ...] = ("leave:read",),
        requires_enterprise_subject: bool = False,
        required_enterprise_subject_type: str | None = None,
        authentication: str = "accept",
        authorization: str = "allow",
        failure_reason: str | None = None,
        oauth_error: str | None = None,
    ) -> None:
        token_file = f"tokens/{case_id}.jwt"
        token_files[token_file] = f"{token}\n"
        normalized = _normalize_claims(claims) if authentication == "accept" and claims is not None else None
        cases.append(
            {
                "expected": _expected(
                    authentication=authentication,
                    authorization=authorization,
                    failure_reason=failure_reason,
                    normalized_claims=normalized,
                    oauth_error=oauth_error,
                ),
                "expected_profile": expected_profile,
                "id": case_id,
                "jwks_file": jwks_file,
                "request_context": {
                    "expected_resource": expected_resource,
                    "expected_tenant": expected_tenant,
                    "required_enterprise_subject_type": required_enterprise_subject_type,
                    "required_scopes": list(required_scopes),
                    "requires_enterprise_subject": requires_enterprise_subject,
                },
                "token_file": token_file,
            }
        )

    valid_claims = _access_claims()
    valid_token = _sign(_header(), valid_claims, ACCESS_CURRENT_KEY)
    add_case("access_valid", valid_token, claims=valid_claims)

    old_claims = _access_claims(jti="access-jti-test-old-key-01")
    add_case(
        "access_valid_old_kid",
        _sign(_header(kid=ACCESS_OLD_KID), old_claims, ACCESS_OLD_KEY),
        claims=old_claims,
    )

    broad_claims = _access_claims(jti="access-jti-test-broad-0001", scope="leave:read leave:submit")
    add_case(
        "access_valid_extra_registered_scope",
        _sign(_header(), broad_claims, ACCESS_CURRENT_KEY),
        claims=broad_claims,
    )

    enterprise_subject = {
        "issuer": "https://hr.example",
        "subject": "subject-test-only",
        "tenant": "issuer-tenant-test",
        "type": "workcode",
    }
    enterprise_claims = _access_claims(
        acr="urn:multirag:assurance:enterprise-verified",
        amr=["channel_event", "directory_lookup"],
        auth_time=VALIDATION_TIME - 120,
        enterprise_subject=enterprise_subject,
        jti="access-jti-test-enterprise1",
    )
    add_case(
        "access_valid_enterprise_subject",
        _sign(_header(), enterprise_claims, ACCESS_CURRENT_KEY),
        claims=enterprise_claims,
        required_enterprise_subject_type="workcode",
        requires_enterprise_subject=True,
    )

    provider_claims = _access_claims(
        acr="urn:multirag:assurance:enterprise-verified",
        amr=["channel_event", "directory_lookup"],
        auth_time=VALIDATION_TIME - 120,
        jti="access-jti-test-provider-0001",
        provider_identity=copy.deepcopy(PROVIDER_IDENTITY),
    )
    add_case(
        "access_valid_provider_identity",
        _sign(_header(), provider_claims, ACCESS_CURRENT_KEY),
        claims=provider_claims,
    )

    add_case(
        "access_missing_required_scope",
        valid_token,
        claims=valid_claims,
        required_scopes=("leave:submit",),
        authorization="deny",
        failure_reason="required_scope_missing",
        oauth_error="insufficient_scope",
    )
    add_case(
        "access_missing_enterprise_subject_when_required",
        valid_token,
        claims=valid_claims,
        required_enterprise_subject_type="workcode",
        requires_enterprise_subject=True,
        authorization="deny",
        failure_reason="enterprise_subject_required",
    )
    add_case(
        "access_wrong_enterprise_subject_type",
        _sign(_header(), enterprise_claims, ACCESS_CURRENT_KEY),
        claims=enterprise_claims,
        required_enterprise_subject_type="talent_id",
        requires_enterprise_subject=True,
        authorization="deny",
        failure_reason="enterprise_subject_type_mismatch",
    )
    add_case(
        "access_tenant_context_mismatch",
        valid_token,
        claims=valid_claims,
        expected_tenant="tenant-test-b",
        authorization="deny",
        failure_reason="tenant_mismatch",
    )

    nbf_boundary_claims = _access_claims(
        exp=VALIDATION_TIME + CLOCK_SKEW_SECONDS + 300,
        iat=VALIDATION_TIME + CLOCK_SKEW_SECONDS,
        jti="access-jti-test-skew-bound1",
        nbf=VALIDATION_TIME + CLOCK_SKEW_SECONDS,
    )
    add_case(
        "access_nbf_at_positive_skew_boundary",
        _sign(_header(), nbf_boundary_claims, ACCESS_CURRENT_KEY),
        claims=nbf_boundary_claims,
    )

    invalid_signed_cases: list[tuple[str, JsonObject, str]] = [
        ("access_expired", _access_claims(exp=VALIDATION_TIME - 31, jti="access-jti-test-expired001"), "token_expired"),
        (
            "access_exp_at_negative_skew_boundary",
            _access_claims(exp=VALIDATION_TIME - CLOCK_SKEW_SECONDS, jti="access-jti-test-exp-bound1"),
            "token_expired",
        ),
        (
            "access_issued_in_future",
            _access_claims(
                exp=VALIDATION_TIME + CLOCK_SKEW_SECONDS + 301,
                iat=VALIDATION_TIME + CLOCK_SKEW_SECONDS + 1,
                jti="access-jti-test-future-iat1",
                nbf=VALIDATION_TIME + CLOCK_SKEW_SECONDS + 1,
            ),
            "token_issued_in_future",
        ),
        (
            "access_not_yet_valid",
            _access_claims(
                exp=VALIDATION_TIME + 240,
                iat=VALIDATION_TIME - 60,
                jti="access-jti-test-future-nbf1",
                nbf=VALIDATION_TIME + CLOCK_SKEW_SECONDS + 1,
            ),
            "token_not_yet_valid",
        ),
        (
            "access_exp_not_after_iat",
            _access_claims(exp=VALIDATION_TIME - 60, jti="access-jti-test-exp-order1"),
            "token_time_order_invalid",
        ),
        (
            "access_ttl_too_long",
            _access_claims(exp=VALIDATION_TIME + 241, jti="access-jti-test-ttl-long01"),
            "token_ttl_exceeded",
        ),
        ("access_wrong_issuer", _access_claims(iss="https://wrong-issuer.example", jti="access-jti-test-wrong-iss1"), "issuer_invalid"),
        ("access_wrong_audience", _access_claims(aud="https://wrong-resource.example/mcp", jti="access-jti-test-wrong-aud1"), "audience_invalid"),
        ("access_audience_list", _access_claims(aud=[GATEWAY_RESOURCE], jti="access-jti-test-aud-list01"), "audience_invalid"),
        ("access_audience_trailing_slash", _access_claims(aud=f"{GATEWAY_RESOURCE}/", jti="access-jti-test-aud-slash1"), "audience_invalid"),
        ("access_unknown_scope", _access_claims(jti="access-jti-test-unknown-sc1", scope="leave:unknown"), "scope_not_registered"),
        ("access_duplicate_scope", _access_claims(jti="access-jti-test-dup-scope1", scope="leave:read leave:read"), "scope_invalid"),
        ("access_malformed_scope", _access_claims(jti="access-jti-test-bad-space1", scope="leave:read  leave:submit"), "scope_invalid"),
        ("access_scope_wrong_type", _access_claims(jti="access-jti-test-scope-type1", scope=["leave:read"]), "claim_type_invalid"),
        ("access_iat_wrong_type", _access_claims(iat="1786492740", jti="access-jti-test-iat-type01"), "claim_type_invalid"),
        ("access_exp_bool", _access_claims(exp=True, jti="access-jti-test-exp-bool01"), "claim_type_invalid"),
        ("access_empty_subject", _access_claims(jti="access-jti-test-empty-sub1", sub=""), "claim_value_invalid"),
        ("access_wrong_token_use", _access_claims(jti="access-jti-test-wrong-use1", token_use="id_token"), "token_use_invalid"),
        (
            "access_auth_time_after_iat",
            _access_claims(auth_time=VALIDATION_TIME - 59, jti="access-jti-test-auth-time1"),
            "auth_time_invalid",
        ),
        ("access_acr_empty", _access_claims(acr="", jti="access-jti-test-acr-empty1"), "acr_invalid"),
        (
            "access_acr_unknown",
            _access_claims(acr="urn:multirag:assurance:unknown", jti="access-jti-test-acr-unknown"),
            "acr_invalid",
        ),
        ("access_amr_empty", _access_claims(amr=[], jti="access-jti-test-amr-empty1"), "amr_invalid"),
        (
            "access_amr_duplicate",
            _access_claims(amr=["channel_event", "channel_event"], jti="access-jti-test-amr-dup001"),
            "amr_invalid",
        ),
        (
            "access_enterprise_subject_invalid",
            _access_claims(enterprise_subject={"issuer": "https://hr.example", "subject": "subject-test-only", "tenant": "issuer-tenant-test"}, jti="access-jti-test-subjectbad1"),
            "enterprise_subject_invalid",
        ),
        (
            "access_provider_identity_invalid",
            _access_claims(
                jti="access-jti-test-provider-bad1",
                provider_identity={
                    "provider": "feishu",
                    "provider_tenant": "provider-tenant-test-a",
                    "subject_type": "user_id",
                },
            ),
            "provider_identity_invalid",
        ),
        ("access_unknown_claim", _access_claims(jti="access-jti-test-unknown-cl1", unexpected_claim="not-allowed"), "claim_not_allowed"),
    ]
    for case_id, claims, reason in invalid_signed_cases:
        add_case(
            case_id,
            _sign(_header(), claims, ACCESS_CURRENT_KEY),
            authentication="reject",
            failure_reason=reason,
        )

    missing_agent_claims = _access_claims(jti="access-jti-test-missing-agent")
    del missing_agent_claims["agent_id"]
    add_case(
        "access_missing_required_claim",
        _sign(_header(), missing_agent_claims, ACCESS_CURRENT_KEY),
        authentication="reject",
        failure_reason="required_claim_missing",
    )

    unknown_critical_header = _header()
    unknown_critical_header["crit"] = ["urn:example:unsupported"]
    unknown_critical_header["urn:example:unsupported"] = True
    jose_header_cases: list[tuple[str, JsonObject, str]] = [
        ("access_missing_typ", {"alg": "ES256", "kid": ACCESS_CURRENT_KID}, "token_type_invalid"),
        ("access_wrong_typ", _header(typ="JWT"), "token_type_invalid"),
        ("access_missing_kid", {"alg": "ES256", "typ": "at+jwt"}, "kid_missing"),
        ("access_unknown_kid", _header(kid="test-only-eim-a1-unknown"), "kid_unknown"),
        ("access_header_jku", _header(jku="https://attacker.example/jwks.json"), "protected_header_forbidden"),
        ("access_header_x5u", _header(x5u="https://attacker.example/cert.pem"), "protected_header_forbidden"),
        ("access_header_jwk", _header(jwk=access_current_jwk), "protected_header_forbidden"),
        ("access_header_unknown_crit", unknown_critical_header, "protected_header_forbidden"),
    ]
    for case_id, header, reason in jose_header_cases:
        add_case(
            case_id,
            _sign(header, valid_claims, ACCESS_CURRENT_KEY),
            authentication="reject",
            failure_reason=reason,
        )

    add_case(
        "access_alg_none",
        _unsigned_none_token(valid_claims),
        authentication="reject",
        failure_reason="algorithm_not_allowed",
    )
    add_case(
        "access_alg_hs256_confusion",
        _hs256_token(valid_claims),
        authentication="reject",
        failure_reason="algorithm_not_allowed",
    )
    rs_header = {"alg": "RS256", "kid": ACCESS_CURRENT_KID, "typ": "at+jwt"}
    add_case(
        "access_alg_rs256",
        f"{_b64url(_canonical_json(rs_header))}.{_b64url(_canonical_json(valid_claims))}.{_b64url(b'test-only-rs-signature')}",
        authentication="reject",
        failure_reason="algorithm_not_allowed",
    )

    valid_parts = valid_token.split(".")
    tampered_payload = _access_claims(jti="access-jti-test-tampered01")
    add_case(
        "access_tampered_payload",
        f"{valid_parts[0]}.{_b64url(_canonical_json(tampered_payload))}.{valid_parts[2]}",
        authentication="reject",
        failure_reason="signature_invalid",
    )
    spaced_header = json.dumps(_header(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    add_case(
        "access_tampered_header",
        f"{_b64url(spaced_header)}.{valid_parts[1]}.{valid_parts[2]}",
        authentication="reject",
        failure_reason="signature_invalid",
    )
    signature = bytearray(base64.urlsafe_b64decode(valid_parts[2] + "=="))
    signature[-1] ^= 1
    add_case(
        "access_tampered_signature",
        f"{valid_parts[0]}.{valid_parts[1]}.{_b64url(bytes(signature))}",
        authentication="reject",
        failure_reason="signature_invalid",
    )

    duplicate_header_json = _canonical_json(_header()).replace(
        f'"kid":"{ACCESS_CURRENT_KID}"'.encode(),
        f'"kid":"{ACCESS_CURRENT_KID}","kid":"{ACCESS_CURRENT_KID}"'.encode(),
    )
    add_case(
        "access_duplicate_header_member",
        _sign_raw(duplicate_header_json, _canonical_json(valid_claims), ACCESS_CURRENT_KEY),
        authentication="reject",
        failure_reason="protected_header_invalid",
    )
    duplicate_payload_json = _canonical_json(valid_claims).replace(
        b'"jti":"access-jti-test-00000001"',
        b'"jti":"access-jti-test-00000001","jti":"access-jti-test-duplicate"',
    )
    add_case(
        "access_duplicate_payload_member",
        _sign_raw(_canonical_json(_header()), duplicate_payload_json, ACCESS_CURRENT_KEY),
        authentication="reject",
        failure_reason="payload_invalid",
    )
    add_case(
        "access_malformed_compact",
        "not-a-compact-token",
        authentication="reject",
        failure_reason="compact_serialization_invalid",
    )
    add_case(
        "access_malformed_base64url",
        f"*.{valid_parts[1]}.{valid_parts[2]}",
        authentication="reject",
        failure_reason="compact_serialization_invalid",
    )
    add_case(
        "access_malformed_header_json",
        f"{_b64url(b'{')}.{valid_parts[1]}.{valid_parts[2]}",
        authentication="reject",
        failure_reason="protected_header_invalid",
    )
    add_case(
        "access_malformed_payload_json",
        f"{valid_parts[0]}.{_b64url(b'{')}.{valid_parts[2]}",
        authentication="reject",
        failure_reason="payload_invalid",
    )
    oversized_claims = _access_claims(jti="access-jti-test-oversized01", padding="x" * 5000)
    add_case(
        "access_oversized",
        _sign(_header(), oversized_claims, ACCESS_CURRENT_KEY),
        authentication="reject",
        failure_reason="token_too_large",
    )

    jwks_negative_cases = {
        "access_jwks_duplicate_kid": ("jwks/invalid/duplicate_kid.json", "jwks_invalid"),
        "access_jwks_private_material": ("jwks/invalid/private_material.json", "jwks_private_material"),
        "access_jwks_wrong_alg": ("jwks/invalid/wrong_alg.json", "jwks_invalid"),
        "access_jwks_wrong_crv": ("jwks/invalid/wrong_crv.json", "jwks_invalid"),
        "access_jwks_wrong_kty": ("jwks/invalid/wrong_kty.json", "jwks_invalid"),
        "access_jwks_wrong_use": ("jwks/invalid/wrong_use.json", "jwks_invalid"),
    }
    for case_id, (jwks_file, reason) in jwks_negative_cases.items():
        add_case(
            case_id,
            valid_token,
            jwks_file=jwks_file,
            authentication="reject",
            failure_reason=reason,
        )

    inbound_claims = _access_claims(aud=INBOUND_RESOURCE, jti="access-jti-test-inbound001", scope="rag:query")
    inbound_token = _sign(_header(), inbound_claims, ACCESS_CURRENT_KEY)
    add_case(
        "inbound_token_at_ofmcp_gateway",
        inbound_token,
        authentication="reject",
        failure_reason="audience_invalid",
    )
    add_case(
        "gateway_token_at_multirag_inbound",
        valid_token,
        expected_resource="multirag_inbound",
        required_scopes=("rag:query",),
        authentication="reject",
        failure_reason="audience_invalid",
    )
    add_case(
        "channel_workload_credential_at_gateway",
        "channel-workload-credential-test-only",
        authentication="reject",
        failure_reason="compact_serialization_invalid",
    )

    parent_claims = broad_claims
    valid_actor_claims = _internal_claims(parent_jti=parent_claims["jti"])
    valid_actor_token = _sign(_header(kid=INTERNAL_KID), valid_actor_claims, INTERNAL_KEY)
    add_case(
        "internal_actor_valid",
        valid_actor_token,
        claims=valid_actor_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
    )
    enterprise_actor_claims = _internal_claims(
        acr=enterprise_claims["acr"],
        amr=enterprise_claims["amr"],
        auth_time=enterprise_claims["auth_time"],
        enterprise_subject=enterprise_claims["enterprise_subject"],
        jti="actor-jti-test-enterprise01",
        parent_jti=enterprise_claims["jti"],
    )
    add_case(
        "internal_actor_valid_enterprise_subject",
        _sign(_header(kid=INTERNAL_KID), enterprise_actor_claims, INTERNAL_KEY),
        claims=enterprise_actor_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
        required_enterprise_subject_type="workcode",
        requires_enterprise_subject=True,
    )
    provider_actor_claims = _internal_claims(
        acr=provider_claims["acr"],
        amr=provider_claims["amr"],
        auth_time=provider_claims["auth_time"],
        jti="actor-jti-test-provider-0001",
        parent_jti=provider_claims["jti"],
        provider_identity=provider_claims["provider_identity"],
    )
    add_case(
        "internal_actor_valid_provider_identity",
        _sign(_header(kid=INTERNAL_KID), provider_actor_claims, INTERNAL_KEY),
        claims=provider_actor_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
    )
    add_case(
        "internal_actor_token_at_gateway",
        valid_actor_token,
        authentication="reject",
        failure_reason="kid_unknown",
    )
    add_case(
        "access_token_at_proxy",
        valid_token,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
        authentication="reject",
        failure_reason="kid_unknown",
    )
    hybrid_access_at_proxy = _internal_claims(parent_jti=parent_claims["jti"], token_use="mcp_access")
    add_case(
        "access_profile_hybrid_at_proxy",
        _sign(_header(kid=INTERNAL_KID), hybrid_access_at_proxy, INTERNAL_KEY),
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
        authentication="reject",
        failure_reason="token_use_invalid",
    )
    hybrid_actor_at_gateway = _access_claims(
        act={"sub": "ofmcp-gateway-test-workload"},
        jti="access-jti-test-hybrid-actor",
        parent_jti_hash=_b64url(hashlib.sha256(parent_claims["jti"].encode()).digest()),
        token_use="mcp_internal_actor",
        trace_id="trace-test-0000000000000002",
    )
    add_case(
        "internal_profile_hybrid_at_gateway",
        _sign(_header(), hybrid_actor_at_gateway, ACCESS_CURRENT_KEY),
        authentication="reject",
        failure_reason="token_use_invalid",
    )
    add_case(
        "internal_actor_service_a_at_service_b",
        valid_actor_token,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="medic_proxy",
        required_scopes=("medic:submit",),
        authentication="reject",
        failure_reason="audience_invalid",
    )
    actor_ttl_long_claims = _internal_claims(
        exp=VALIDATION_TIME + 56,
        parent_jti=parent_claims["jti"],
        jti="actor-jti-test-ttl-long001",
    )
    add_case(
        "internal_actor_ttl_too_long",
        _sign(_header(kid=INTERNAL_KID), actor_ttl_long_claims, INTERNAL_KEY),
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
        authentication="reject",
        failure_reason="token_ttl_exceeded",
    )
    actor_missing_act_claims = _internal_claims(parent_jti=parent_claims["jti"], jti="actor-jti-test-no-act-0001")
    del actor_missing_act_claims["act"]
    add_case(
        "internal_actor_missing_act",
        _sign(_header(kid=INTERNAL_KID), actor_missing_act_claims, INTERNAL_KEY),
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
        authentication="reject",
        failure_reason="required_claim_missing",
    )

    parent_narrow_claims = _access_claims(jti="access-jti-test-parent-narrow", scope="leave:read")
    add_case(
        "access_parent_narrow",
        _sign(_header(), parent_narrow_claims, ACCESS_CURRENT_KEY),
        claims=parent_narrow_claims,
    )
    actor_expanded_claims = _internal_claims(
        jti="actor-jti-test-scope-expand",
        parent_jti=parent_narrow_claims["jti"],
        scope="leave:read leave:submit",
    )
    add_case(
        "internal_actor_scope_expanded",
        _sign(_header(kid=INTERNAL_KID), actor_expanded_claims, INTERNAL_KEY),
        claims=actor_expanded_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
    )
    parent_short_claims = _access_claims(exp=VALIDATION_TIME + 30, jti="access-jti-test-parent-short1")
    add_case(
        "access_parent_short_lived",
        _sign(_header(), parent_short_claims, ACCESS_CURRENT_KEY),
        claims=parent_short_claims,
    )
    actor_exceeds_parent_claims = _internal_claims(
        jti="actor-jti-test-exp-parent01",
        parent_jti=parent_short_claims["jti"],
    )
    add_case(
        "internal_actor_exceeds_parent_expiry",
        _sign(_header(kid=INTERNAL_KID), actor_exceeds_parent_claims, INTERNAL_KEY),
        claims=actor_exceeds_parent_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
    )
    actor_wrong_parent_hash_claims = _internal_claims(
        jti="actor-jti-test-parent-hash1",
        parent_jti="different-parent-jti-test",
    )
    add_case(
        "internal_actor_wrong_parent_hash",
        _sign(_header(kid=INTERNAL_KID), actor_wrong_parent_hash_claims, INTERNAL_KEY),
        claims=actor_wrong_parent_hash_claims,
        expected_profile="mcp_internal_actor",
        jwks_file="jwks/internal_actor.json",
        expected_resource="leave_proxy",
    )

    issuance_policy_cases.extend(
        [
            {
                "expected": {"failure_reason": "subject_not_platform_principal", "issuance": "reject"},
                "id": "issuer_rejects_provider_external_subject",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_scopes": ["leave:read"],
                    "subject_source": "provider_external_identifier",
                },
            },
            {
                "expected": {"failure_reason": "requested_scope_not_registered", "issuance": "reject"},
                "id": "issuer_rejects_unknown_requested_scope",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_scopes": ["leave:unknown"],
                    "subject_source": "platform_principal",
                },
            },
            {
                "expected": {"failure_reason": "requested_scope_not_granted", "issuance": "reject"},
                "id": "issuer_rejects_scope_elevation",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_scopes": ["leave:submit"],
                    "subject_source": "platform_principal",
                },
            },
            {
                "expected": {"failure_reason": None, "issuance": "allow"},
                "id": "issuer_allows_registered_granted_scope",
                "request": {
                    "allowed_scopes": ["leave:read", "leave:submit"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_scopes": ["leave:read"],
                    "subject_source": "platform_principal",
                },
            },
            {
                "expected": {"failure_reason": "requested_claim_not_allowed", "issuance": "reject"},
                "id": "issuer_rejects_forbidden_channel_and_role_claims",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_claims": ["channel_message", "department", "roles"],
                    "requested_scopes": ["leave:read"],
                    "subject_source": "platform_principal",
                },
            },
            {
                "expected": {"failure_reason": "tenant_not_server_bound", "issuance": "reject"},
                "id": "issuer_rejects_caller_selected_tenant",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_scopes": ["leave:read"],
                    "subject_source": "platform_principal",
                    "tenant_source": "caller_payload",
                },
            },
            {
                "expected": {"failure_reason": "assurance_not_verified", "issuance": "reject"},
                "id": "issuer_rejects_unverified_assurance",
                "request": {
                    "allowed_scopes": ["leave:read"],
                    "assurance_verified": False,
                    "registered_scopes": ["leave:read", "leave:submit"],
                    "requested_claims": ["enterprise_subject"],
                    "requested_scopes": ["leave:read"],
                    "requires_assurance": True,
                    "subject_source": "platform_principal",
                },
            },
        ]
    )
    for policy_case in issuance_policy_cases:
        request = policy_case["request"]
        request.setdefault("assurance_verified", True)
        request.setdefault("requested_claims", [])
        request.setdefault("requires_assurance", False)
        request.setdefault("tenant_source", "server_context")
    delegation_cases.extend(
        [
            {
                "actor_case_id": "internal_actor_valid",
                "expected": {"delegation": "allow", "failure_reason": None},
                "id": "actor_valid_scope_attenuation",
                "parent_case_id": "access_valid_extra_registered_scope",
                "required_preserved_claims": [],
            },
            {
                "actor_case_id": "internal_actor_valid_enterprise_subject",
                "expected": {"delegation": "allow", "failure_reason": None},
                "id": "actor_preserves_enterprise_assurance",
                "parent_case_id": "access_valid_enterprise_subject",
                "required_preserved_claims": ["acr", "amr", "auth_time", "enterprise_subject"],
            },
            {
                "actor_case_id": "internal_actor_valid_provider_identity",
                "expected": {"delegation": "allow", "failure_reason": None},
                "id": "actor_preserves_provider_identity",
                "parent_case_id": "access_valid_provider_identity",
                "required_preserved_claims": ["acr", "amr", "auth_time", "provider_identity"],
            },
            {
                "actor_case_id": "internal_actor_scope_expanded",
                "expected": {"delegation": "reject", "failure_reason": "actor_scope_not_attenuated"},
                "id": "actor_rejects_scope_expansion",
                "parent_case_id": "access_parent_narrow",
                "required_preserved_claims": [],
            },
            {
                "actor_case_id": "internal_actor_exceeds_parent_expiry",
                "expected": {"delegation": "reject", "failure_reason": "actor_expiry_exceeds_parent"},
                "id": "actor_rejects_expiry_beyond_parent",
                "parent_case_id": "access_parent_short_lived",
                "required_preserved_claims": [],
            },
            {
                "actor_case_id": "internal_actor_wrong_parent_hash",
                "expected": {"delegation": "reject", "failure_reason": "actor_parent_jti_hash_mismatch"},
                "id": "actor_rejects_wrong_parent_hash",
                "parent_case_id": "access_valid_extra_registered_scope",
                "required_preserved_claims": [],
            },
        ]
    )

    access_required = ["agent_id", "aud", "client_id", "exp", "iat", "iss", "jti", "nbf", "scope", "sub", "tenant_id", "token_use"]
    access_allowed = [
        *access_required,
        "acr",
        "amr",
        "auth_time",
        "enterprise_subject",
        "provider_identity",
    ]
    internal_required = [
        "act",
        "agent_id",
        "aud",
        "client_id",
        "exp",
        "iat",
        "iss",
        "jti",
        "nbf",
        "parent_jti_hash",
        "scope",
        "sub",
        "tenant_id",
        "token_use",
        "trace_id",
    ]
    manifest: JsonObject = {
        "cases": cases,
        "clock_skew_seconds": CLOCK_SKEW_SECONDS,
        "contract_version": CONTRACT_VERSION,
        "delegation_cases": delegation_cases,
        "issuance_policy_cases": issuance_policy_cases,
        "max_token_bytes": MAX_TOKEN_BYTES,
        "profiles": {
            "mcp_access": {
                "allowed_acr_values": ["urn:multirag:assurance:enterprise-verified"],
                "allowed_amr_values": ["channel_event", "directory_lookup", "oidc", "password"],
                "allowed_claims": sorted(access_allowed),
                "issuer": ACCESS_ISSUER,
                "max_ttl_seconds": 300,
                "required_claims": sorted(access_required),
                "token_use": "mcp_access",
            },
            "mcp_internal_actor": {
                "allowed_acr_values": ["urn:multirag:assurance:enterprise-verified"],
                "allowed_amr_values": ["channel_event", "directory_lookup", "oidc", "password"],
                "allowed_claims": sorted(
                    [
                        *internal_required,
                        "acr",
                        "amr",
                        "auth_time",
                        "enterprise_subject",
                        "provider_identity",
                    ]
                ),
                "issuer": INTERNAL_ISSUER,
                "max_ttl_seconds": 60,
                "required_claims": sorted(internal_required),
                "token_use": "mcp_internal_actor",
            },
        },
        "resources": {
            "leave_proxy": {"profile": "mcp_internal_actor", "uri": LEAVE_PROXY_RESOURCE},
            "medic_proxy": {"profile": "mcp_internal_actor", "uri": MEDIC_PROXY_RESOURCE},
            "multirag_inbound": {"profile": "mcp_access", "uri": INBOUND_RESOURCE},
            "ofmcp_gateway": {"profile": "mcp_access", "uri": GATEWAY_RESOURCE},
        },
        "scope_registry": {
            "leave_proxy": ["leave:read", "leave:submit"],
            "medic_proxy": ["medic:submit"],
            "multirag_inbound": ["rag:query"],
            "ofmcp_gateway": ["leave:read", "leave:submit", "medic:submit"],
        },
        "validation_time": VALIDATION_TIME,
    }
    return manifest, token_files, jwks_files


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: JsonObject) -> None:
    _write_text(path, f"{json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}\n")


def _clean_generated_files(output: Path) -> None:
    for pattern in ("tokens/*.jwt", "jwks/*.json", "jwks/invalid/*.json"):
        for path in output.glob(pattern):
            if path.is_file():
                path.unlink()
    for name in ("README.md", "manifest.schema.json", "manifest.json", "SHA256SUMS"):
        path = output / name
        if path.is_file():
            path.unlink()


def _validate_output_directory(output: Path) -> Path:
    """Return a safe output path before any generated file is removed.

    The checked-in corpus is the only non-empty directory this maintainer tool
    may replace.  A custom output is useful for reproducibility checks, but it
    must be a new or empty directory so a typo cannot delete an unrelated
    README, manifest, token fixture, or JWKS tree.
    """

    absolute = output.expanduser().absolute()
    resolved = absolute.resolve(strict=False)
    forbidden = {Path(resolved.anchor), REPOSITORY_ROOT.resolve(), Path.home().resolve()}
    if resolved in forbidden:
        raise ValueError(f"refusing unsafe EIM-A1 output directory: {absolute}")
    if absolute.is_symlink():
        raise ValueError(f"refusing symlink EIM-A1 output directory: {absolute}")
    if absolute.exists() and not absolute.is_dir():
        raise ValueError(f"EIM-A1 output is not a directory: {absolute}")

    canonical = DEFAULT_OUTPUT.resolve()
    if resolved != canonical and absolute.exists() and any(absolute.iterdir()):
        raise ValueError(f"custom EIM-A1 output directory must be empty: {absolute}")
    if absolute.exists():
        symlink = next((path for path in absolute.rglob("*") if path.is_symlink()), None)
        if symlink is not None:
            raise ValueError(f"refusing EIM-A1 output containing symlink: {symlink}")
    return absolute


def _write_corpus(output: Path) -> None:
    output = _validate_output_directory(output)
    _clean_generated_files(output)
    manifest, token_files, jwks_files = _build_corpus()
    generated_files = [output / "README.md", output / "manifest.schema.json", output / "manifest.json"]
    _write_text(generated_files[0], _readme())
    _write_json(generated_files[1], _manifest_schema())
    _write_json(generated_files[2], manifest)
    for relative_path, jwk_document in sorted(jwks_files.items()):
        path = output / relative_path
        _write_json(path, jwk_document)
        generated_files.append(path)
    for relative_path, token_text in sorted(token_files.items()):
        path = output / relative_path
        _write_text(path, token_text)
        generated_files.append(path)

    checksums: list[str] = []
    for path in sorted(generated_files):
        relative_path = path.relative_to(output).as_posix()
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative_path}")
    _write_text(output / "SHA256SUMS", f"{'\n'.join(checksums)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="corpus output directory")
    args = parser.parse_args()
    _write_corpus(args.output)


if __name__ == "__main__":
    main()
