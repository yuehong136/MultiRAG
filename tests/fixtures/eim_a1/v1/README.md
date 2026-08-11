# EIM-A1 deterministic JWT/JWKS corpus

This directory is the language-neutral `eim-a1/v1` contract shared byte-for-byte by
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
