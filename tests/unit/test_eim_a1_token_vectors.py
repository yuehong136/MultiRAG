"""Independent PyJWT consumer for the language-neutral EIM-A1 corpus.

This is a contract oracle, not the EIM-A2 production issuer or the EIM-A3
Resource Server. PyJWT verifies ES256, issuer, and exact single audience. The
project profile layer deliberately owns strict JOSE/JWKS shape, typed claims,
fixed-clock lifetime rules, authorization classification, and delegation
relations that a generic JWT library cannot infer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import jwt
import pytest
from jsonschema import Draft202012Validator
from jwt.exceptions import InvalidAudienceError, InvalidIssuerError, InvalidSignatureError, InvalidTokenError

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "eim_a1" / "v2"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
SCHEMA_PATH = CORPUS_ROOT / "manifest.schema.json"

JsonObject = dict[str, Any]
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
# RFC 6749 scope-token: visible ASCII except DQUOTE and reverse solidus.
_SCOPE_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,128}$")
_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ALLOWED_JWK_MEMBERS = {"alg", "crv", "kid", "kty", "use", "x", "y"}
_PRIVATE_JWK_MEMBERS = {"d", "dp", "dq", "k", "oth", "p", "q", "qi"}
_FORBIDDEN_TOKEN_CLAIMS = {
    "channel_message",
    "chat_id",
    "confirmation",
    "department",
    "email",
    "groups",
    "name",
    "open_id",
    "phone",
    "prompt",
    "provider_access_token",
    "provider_user_id",
    "roles",
    "union_id",
}


class DuplicateJsonMember(ValueError):
    """Raised when security-sensitive JSON contains duplicate object names."""


class VectorRejected(ValueError):
    """Stable, library-neutral EIM-A1 validation failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ParsedToken:
    compact: str
    header: JsonObject
    payload: JsonObject


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for name, value in pairs:
        if name in result:
            raise DuplicateJsonMember(name)
        result[name] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(raw: str | bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_reject_duplicate_members, parse_constant=_reject_json_constant)


def _load_json_object(path: Path) -> JsonObject:
    value = _strict_json_loads(path.read_bytes())
    assert isinstance(value, dict), f"expected JSON object: {path}"
    return value


MANIFEST = _load_json_object(MANIFEST_PATH)


def _resolve_corpus_path(relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise AssertionError(f"unsafe corpus path: {relative!r}")
    path = (CORPUS_ROOT / Path(*pure.parts)).resolve()
    if not path.is_relative_to(CORPUS_ROOT.resolve()):
        raise AssertionError(f"corpus path escapes root: {relative!r}")
    return path


def _read_compact_token(relative: str) -> str:
    raw = _resolve_corpus_path(relative).read_bytes()
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise AssertionError(f"token fixture must end in exactly one LF: {relative}")
    raw = raw[:-1]
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise VectorRejected("compact_serialization_invalid") from exc


def _b64url_decode(segment: str, *, allow_empty: bool = False) -> bytes:
    if (not segment and not allow_empty) or not _BASE64URL_RE.fullmatch(segment):
        raise ValueError("invalid base64url segment")
    padding = "=" * (-len(segment) % 4)
    decoded = base64.urlsafe_b64decode(f"{segment}{padding}")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != segment:
        raise ValueError("non-canonical base64url segment")
    return decoded


def _parse_json_segment(segment: str, reason: str) -> JsonObject:
    try:
        raw = _b64url_decode(segment)
        value = _strict_json_loads(raw)
    except (DuplicateJsonMember, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise VectorRejected(reason) from exc
    if not isinstance(value, dict):
        raise VectorRejected(reason)
    return value


def _parse_compact_token(compact: str, max_token_bytes: int) -> ParsedToken:
    if len(compact.encode("ascii")) > max_token_bytes:
        raise VectorRejected("token_too_large")
    parts = compact.split(".")
    if len(parts) != 3:
        raise VectorRejected("compact_serialization_invalid")
    try:
        _b64url_decode(parts[0])
        _b64url_decode(parts[1])
        _b64url_decode(parts[2], allow_empty=True)
    except ValueError as exc:
        raise VectorRejected("compact_serialization_invalid") from exc
    header = _parse_json_segment(parts[0], "protected_header_invalid")
    payload = _parse_json_segment(parts[1], "payload_invalid")
    return ParsedToken(compact=compact, header=header, payload=payload)


def _validate_header(header: JsonObject) -> str:
    extra = set(header) - {"alg", "kid", "typ"}
    if extra:
        raise VectorRejected("protected_header_forbidden")
    if header.get("alg") != "ES256":
        raise VectorRejected("algorithm_not_allowed")
    if header.get("typ") != "at+jwt":
        raise VectorRejected("token_type_invalid")
    kid = header.get("kid")
    if kid is None:
        raise VectorRejected("kid_missing")
    if not isinstance(kid, str) or not kid:
        raise VectorRejected("kid_invalid")
    return kid


def _strict_jwk(relative: str, kid: str) -> jwt.PyJWK:
    try:
        jwks = _load_json_object(_resolve_corpus_path(relative))
    except (AssertionError, DuplicateJsonMember, OSError, ValueError, json.JSONDecodeError) as exc:
        raise VectorRejected("jwks_invalid") from exc
    if set(jwks) != {"keys"} or not isinstance(jwks["keys"], list) or not jwks["keys"]:
        raise VectorRejected("jwks_invalid")

    keys_by_id: dict[str, JsonObject] = {}
    for raw_key in jwks["keys"]:
        if not isinstance(raw_key, dict):
            raise VectorRejected("jwks_invalid")
        if _PRIVATE_JWK_MEMBERS.intersection(raw_key):
            raise VectorRejected("jwks_private_material")
        if set(raw_key) != _ALLOWED_JWK_MEMBERS:
            raise VectorRejected("jwks_invalid")
        if raw_key.get("kty") != "EC" or raw_key.get("crv") != "P-256" or raw_key.get("use") != "sig" or raw_key.get("alg") != "ES256":
            raise VectorRejected("jwks_invalid")
        raw_kid = raw_key.get("kid")
        if not isinstance(raw_kid, str) or not raw_kid or raw_kid in keys_by_id:
            raise VectorRejected("jwks_invalid")
        try:
            if len(_b64url_decode(raw_key["x"])) != 32 or len(_b64url_decode(raw_key["y"])) != 32:
                raise ValueError("invalid P-256 coordinate size")
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorRejected("jwks_invalid") from exc
        keys_by_id[raw_kid] = raw_key

    selected = keys_by_id.get(kid)
    if selected is None:
        raise VectorRejected("kid_unknown")
    try:
        return jwt.PyJWK.from_dict(selected, algorithm="ES256")
    except InvalidTokenError as exc:
        raise VectorRejected("jwks_invalid") from exc


def _verify_signature_issuer_audience(parsed: ParsedToken, key: jwt.PyJWK, *, issuer: str, audience: str) -> JsonObject:
    try:
        decoded = jwt.decode(
            parsed.compact,
            key=key,
            algorithms=["ES256"],
            issuer=issuer,
            audience=audience,
            options={
                "strict_aud": True,
                "verify_exp": False,
                "verify_iat": False,
                "verify_jti": False,
                "verify_nbf": False,
                "verify_sub": False,
            },
        )
    except InvalidSignatureError as exc:
        raise VectorRejected("signature_invalid") from exc
    except InvalidIssuerError as exc:
        raise VectorRejected("issuer_invalid") from exc
    except InvalidAudienceError as exc:
        raise VectorRejected("audience_invalid") from exc
    except InvalidTokenError as exc:
        raise VectorRejected("signature_invalid") from exc
    if decoded != parsed.payload:
        raise VectorRejected("payload_invalid")
    return decoded


def _nonempty_string(claims: JsonObject, name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str):
        raise VectorRejected("claim_type_invalid")
    if not value or value != value.strip() or len(value) > 512:
        raise VectorRejected("claim_value_invalid")
    return value


def _numeric_date(claims: JsonObject, name: str) -> int:
    value = claims.get(name)
    if type(value) is not int:
        raise VectorRejected("claim_type_invalid")
    return value


def _parse_scopes(claims: JsonObject, registered: set[str]) -> tuple[str, ...]:
    raw_scope = claims.get("scope")
    if not isinstance(raw_scope, str):
        raise VectorRejected("claim_type_invalid")
    scopes = raw_scope.split(" ")
    if not raw_scope or any(not scope or not _SCOPE_RE.fullmatch(scope) for scope in scopes) or len(scopes) != len(set(scopes)):
        raise VectorRejected("scope_invalid")
    if any(scope not in registered for scope in scopes):
        raise VectorRejected("scope_not_registered")
    return tuple(sorted(scopes))


def _validate_enterprise_subject(claims: JsonObject) -> None:
    subject = claims.get("enterprise_subject")
    if subject is None:
        return
    if not isinstance(subject, dict) or set(subject) != {"issuer", "subject", "tenant", "type"}:
        raise VectorRejected("enterprise_subject_invalid")
    for name in ("issuer", "subject", "tenant", "type"):
        value = subject[name]
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
            raise VectorRejected("enterprise_subject_invalid")
    if not subject["issuer"].startswith("https://"):
        raise VectorRejected("enterprise_subject_invalid")


def _validate_provider_identity(claims: JsonObject) -> None:
    provider_identity = claims.get("provider_identity")
    if provider_identity is None:
        return
    if not isinstance(provider_identity, dict) or set(provider_identity) != {
        "provider",
        "provider_tenant",
        "subject",
        "subject_type",
    }:
        raise VectorRejected("provider_identity_invalid")
    for name in ("provider", "provider_tenant", "subject", "subject_type"):
        value = provider_identity[name]
        max_length = 256 if name in {"provider_tenant", "subject"} else 64
        if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable() or len(value) > max_length:
            raise VectorRejected("provider_identity_invalid")


def _validate_assurance_claims(claims: JsonObject, iat: int, profile: JsonObject) -> None:
    if "auth_time" in claims:
        auth_time = _numeric_date(claims, "auth_time")
        if auth_time > iat:
            raise VectorRejected("auth_time_invalid")
    if "acr" in claims:
        acr = claims.get("acr")
        if not isinstance(acr, str) or not acr or acr not in profile["allowed_acr_values"]:
            raise VectorRejected("acr_invalid")
    if "amr" in claims:
        amr = claims["amr"]
        if not isinstance(amr, list) or not amr:
            raise VectorRejected("amr_invalid")
        if any(not isinstance(method, str) or not method or method not in profile["allowed_amr_values"] for method in amr):
            raise VectorRejected("amr_invalid")
        if len(amr) != len(set(amr)):
            raise VectorRejected("amr_invalid")


def _validate_profile_claims(case: JsonObject, claims: JsonObject) -> JsonObject:
    profile = MANIFEST["profiles"][case["expected_profile"]]
    context = case["request_context"]
    resource_name = context["expected_resource"]
    registered_scopes = set(MANIFEST["scope_registry"][resource_name])

    missing = [name for name in profile["required_claims"] if name not in claims or claims[name] is None]
    if missing:
        raise VectorRejected("required_claim_missing")
    if claims.get("token_use") != profile["token_use"]:
        raise VectorRejected("token_use_invalid")
    if set(claims) - set(profile["allowed_claims"]):
        raise VectorRejected("claim_not_allowed")

    for name in ("agent_id", "client_id", "iss", "jti", "sub", "tenant_id", "token_use"):
        _nonempty_string(claims, name)
    audience = _nonempty_string(claims, "aud")
    if audience != MANIFEST["resources"][resource_name]["uri"]:
        raise VectorRejected("audience_invalid")

    iat = _numeric_date(claims, "iat")
    nbf = _numeric_date(claims, "nbf")
    exp = _numeric_date(claims, "exp")
    now = MANIFEST["validation_time"]
    skew = MANIFEST["clock_skew_seconds"]
    if exp <= iat or exp <= nbf:
        raise VectorRejected("token_time_order_invalid")
    if exp - iat > profile["max_ttl_seconds"]:
        raise VectorRejected("token_ttl_exceeded")
    if iat > now + skew:
        raise VectorRejected("token_issued_in_future")
    if nbf > now + skew:
        raise VectorRejected("token_not_yet_valid")
    if exp <= now - skew:
        raise VectorRejected("token_expired")

    _validate_assurance_claims(claims, iat, profile)
    scopes = _parse_scopes(claims, registered_scopes)
    _validate_enterprise_subject(claims)
    _validate_provider_identity(claims)

    if case["expected_profile"] == "mcp_internal_actor":
        actor = claims.get("act")
        if not isinstance(actor, dict) or set(actor) != {"sub"} or not isinstance(actor["sub"], str) or not actor["sub"]:
            raise VectorRejected("actor_claim_invalid")
        if not _HASH_RE.fullmatch(_nonempty_string(claims, "parent_jti_hash")):
            raise VectorRejected("parent_jti_hash_invalid")
        _nonempty_string(claims, "trace_id")

    normalized = json.loads(json.dumps(claims, ensure_ascii=False))
    del normalized["scope"]
    normalized["scopes"] = list(scopes)
    return normalized


def _authenticate(case: JsonObject) -> JsonObject:
    parsed = _parse_compact_token(_read_compact_token(case["token_file"]), MANIFEST["max_token_bytes"])
    kid = _validate_header(parsed.header)
    key = _strict_jwk(case["jwks_file"], kid)
    profile = MANIFEST["profiles"][case["expected_profile"]]
    resource = MANIFEST["resources"][case["request_context"]["expected_resource"]]["uri"]
    claims = _verify_signature_issuer_audience(parsed, key, issuer=profile["issuer"], audience=resource)
    return _validate_profile_claims(case, claims)


def _authorize(case: JsonObject, normalized: JsonObject) -> tuple[str, str | None, str | None]:
    context = case["request_context"]
    if normalized["tenant_id"] != context["expected_tenant"]:
        return "deny", None, "tenant_mismatch"
    if not set(context["required_scopes"]).issubset(normalized["scopes"]):
        return "deny", "insufficient_scope", "required_scope_missing"
    if context["requires_enterprise_subject"]:
        subject = normalized.get("enterprise_subject")
        if subject is None:
            return "deny", None, "enterprise_subject_required"
        required_type = context["required_enterprise_subject_type"]
        if required_type is not None and subject["type"] != required_type:
            return "deny", None, "enterprise_subject_type_mismatch"
    return "allow", None, None


def _evaluate_token_case(case: JsonObject) -> JsonObject:
    try:
        normalized = _authenticate(case)
    except VectorRejected as exc:
        return {
            "authentication": "reject",
            "authorization": "not_evaluated",
            "failure_reason": exc.reason,
            "http_status": 401,
            "normalized_claims": None,
            "oauth_error": "invalid_token",
        }

    authorization, oauth_error, reason = _authorize(case, normalized)
    if authorization == "deny":
        return {
            "authentication": "accept",
            "authorization": "deny",
            "failure_reason": reason,
            "http_status": 403,
            "normalized_claims": normalized,
            "oauth_error": oauth_error,
        }
    return {
        "authentication": "accept",
        "authorization": "allow",
        "failure_reason": None,
        "http_status": 200,
        "normalized_claims": normalized,
        "oauth_error": None,
    }


def _evaluate_issuance_policy(case: JsonObject) -> JsonObject:
    request = case["request"]
    if request["subject_source"] != "platform_principal":
        return {"failure_reason": "subject_not_platform_principal", "issuance": "reject"}
    if request["tenant_source"] != "server_context":
        return {"failure_reason": "tenant_not_server_bound", "issuance": "reject"}
    if not set(request["requested_claims"]).issubset(MANIFEST["profiles"]["mcp_access"]["allowed_claims"]):
        return {"failure_reason": "requested_claim_not_allowed", "issuance": "reject"}
    requested = set(request["requested_scopes"])
    if not requested.issubset(request["registered_scopes"]):
        return {"failure_reason": "requested_scope_not_registered", "issuance": "reject"}
    if not requested.issubset(request["allowed_scopes"]):
        return {"failure_reason": "requested_scope_not_granted", "issuance": "reject"}
    if request["requires_assurance"] and not request["assurance_verified"]:
        return {"failure_reason": "assurance_not_verified", "issuance": "reject"}
    return {"failure_reason": None, "issuance": "allow"}


def _claims_for_relation(case_by_id: dict[str, JsonObject], case_id: str) -> JsonObject:
    case = case_by_id[case_id]
    normalized = _authenticate(case)
    authorization, _, reason = _authorize(case, normalized)
    if authorization != "allow":
        raise AssertionError(f"delegation fixture {case_id} is not independently authorized: {reason}")
    return normalized


def _evaluate_delegation(case: JsonObject) -> JsonObject:
    case_by_id = {candidate["id"]: candidate for candidate in MANIFEST["cases"]}
    parent = _claims_for_relation(case_by_id, case["parent_case_id"])
    actor = _claims_for_relation(case_by_id, case["actor_case_id"])
    if any(actor[name] != parent[name] for name in ("sub", "tenant_id", "agent_id")):
        return {"delegation": "reject", "failure_reason": "actor_principal_mismatch"}
    if not set(actor["scopes"]).issubset(parent["scopes"]):
        return {"delegation": "reject", "failure_reason": "actor_scope_not_attenuated"}
    if actor["exp"] > parent["exp"]:
        return {"delegation": "reject", "failure_reason": "actor_expiry_exceeds_parent"}
    if actor["iat"] < parent["iat"]:
        return {"delegation": "reject", "failure_reason": "actor_predates_parent"}
    expected_parent_hash = base64.urlsafe_b64encode(hashlib.sha256(parent["jti"].encode("utf-8")).digest()).rstrip(b"=").decode("ascii")
    if actor["parent_jti_hash"] != expected_parent_hash:
        return {"delegation": "reject", "failure_reason": "actor_parent_jti_hash_mismatch"}
    assurance_claims = (
        "acr",
        "amr",
        "auth_time",
        "enterprise_subject",
        "provider_identity",
    )
    if any(name in actor and actor[name] != parent.get(name) for name in assurance_claims):
        return {"delegation": "reject", "failure_reason": "actor_assurance_elevated"}
    if any(actor.get(name) != parent.get(name) for name in case["required_preserved_claims"]):
        return {"delegation": "reject", "failure_reason": "actor_assurance_not_preserved"}
    return {"delegation": "allow", "failure_reason": None}


def _collect_object_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_collect_object_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_collect_object_keys(child))
        return keys
    return set()


def test_eim_a1_manifest_matches_draft_2020_12_schema() -> None:
    schema = _load_json_object(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(MANIFEST), key=lambda error: list(error.absolute_path))
    assert not errors, "\n".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)


def test_eim_a1_manifest_semantics_are_closed_and_cross_references_resolve() -> None:
    cases = MANIFEST["cases"]
    case_ids = [case["id"] for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert set(MANIFEST["resources"]) == set(MANIFEST["scope_registry"])
    for profile in MANIFEST["profiles"].values():
        assert set(profile["required_claims"]).issubset(profile["allowed_claims"])
    for case in cases:
        context = case["request_context"]
        assert MANIFEST["resources"][context["expected_resource"]]["profile"] == case["expected_profile"]
        assert _resolve_corpus_path(case["token_file"]).is_file()
        assert _resolve_corpus_path(case["jwks_file"]).is_file()
        assert context["requires_enterprise_subject"] == (context["required_enterprise_subject_type"] is not None)
        if case["expected"]["authentication"] == "accept":
            assert case["expected"]["normalized_claims"] is not None
        else:
            assert case["expected"]["authorization"] == "not_evaluated"
            assert case["expected"]["http_status"] == 401
            assert case["expected"]["oauth_error"] == "invalid_token"
    for relation in MANIFEST["delegation_cases"]:
        assert relation["parent_case_id"] in case_ids
        assert relation["actor_case_id"] in case_ids
        assert set(relation["required_preserved_claims"]).issubset({"acr", "amr", "auth_time", "enterprise_subject", "provider_identity"})


def test_eim_a1_sha256sums_are_complete_sorted_and_correct() -> None:
    checksum_path = CORPUS_ROOT / "SHA256SUMS"
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    entries: dict[str, str] = {}
    for line in lines:
        digest, relative = line.split("  ", 1)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert relative not in entries
        entries[relative] = digest
    corpus_files = {path.relative_to(CORPUS_ROOT).as_posix() for path in CORPUS_ROOT.rglob("*") if path.is_file() and path != checksum_path}
    assert set(entries) == corpus_files
    for relative, digest in entries.items():
        assert hashlib.sha256(_resolve_corpus_path(relative).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda case: case["id"])
def test_eim_a1_token_case(case: JsonObject) -> None:
    assert _evaluate_token_case(case) == case["expected"]


@pytest.mark.parametrize("case", MANIFEST["issuance_policy_cases"], ids=lambda case: case["id"])
def test_eim_a1_issuance_policy_case(case: JsonObject) -> None:
    assert _evaluate_issuance_policy(case) == case["expected"]


@pytest.mark.parametrize("case", MANIFEST["delegation_cases"], ids=lambda case: case["id"])
def test_eim_a1_delegation_case(case: JsonObject) -> None:
    assert _evaluate_delegation(case) == case["expected"]


def test_eim_a1_tokens_have_no_provider_identity_or_sensitive_claim_names() -> None:
    observed_keys: set[str] = set()
    for case in MANIFEST["cases"]:
        compact = _read_compact_token(case["token_file"])
        parts = compact.split(".")
        if len(parts) != 3:
            continue
        try:
            payload = _parse_json_segment(parts[1], "payload_invalid")
        except VectorRejected:
            continue
        observed_keys.update(_collect_object_keys(payload))
    assert observed_keys.isdisjoint(_FORBIDDEN_TOKEN_CLAIMS)


def test_eim_a1_profiles_use_distinct_issuers_keysets_and_resources() -> None:
    assert MANIFEST["profiles"]["mcp_access"]["issuer"] != MANIFEST["profiles"]["mcp_internal_actor"]["issuer"]
    access_jwks = _load_json_object(CORPUS_ROOT / "jwks" / "access.json")
    internal_jwks = _load_json_object(CORPUS_ROOT / "jwks" / "internal_actor.json")
    access_kids = {key["kid"] for key in access_jwks["keys"]}
    internal_kids = {key["kid"] for key in internal_jwks["keys"]}
    assert access_kids.isdisjoint(internal_kids)
    access_resources = {item["uri"] for item in MANIFEST["resources"].values() if item["profile"] == "mcp_access"}
    internal_resources = {item["uri"] for item in MANIFEST["resources"].values() if item["profile"] == "mcp_internal_actor"}
    assert access_resources.isdisjoint(internal_resources)
