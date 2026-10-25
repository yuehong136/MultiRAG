# Go connector metadata

The Go parallel implementation exposes `GET /api/v1/connectors/{connector_id}`
through its existing authenticated router. It uses the current JWT/API-token,
superuser and license checks. This does not change the Python production API.

The current connector resource policy permits the tenant owner and active
`owner`, `admin` and `normal` tenant memberships. A member's user ID can differ
from the tenant ID. Disabled memberships and invitation roles grant no access.
Unknown and inaccessible IDs both return business code `109`, `data: null`, and
`message: "No authorization."` in the existing HTTP 200 envelope.

The response contains `id`, `tenant_id`, `name`, `source`, `input_type`,
`refresh_freq`, `prune_freq`, `timeout_secs`, `status`, and optional
`indexing_start` in UTC. Zero schedule values are preserved. The SQL projection
never selects `config`; the DTO excludes credentials and source configuration.
The endpoint reads metadata only and does not probe or synchronize a source.

The integration test `TestConnectorDetailHTTPJWTAndSQL` uses the real router,
signed JWTs, a loopback HTTP listener, and an owned PostgreSQL scratch database.
Set `REQUIRE_SERVICES=1` with `MULTIRAG_CONNECTOR_DETAIL_DSN` (database name must
start with `multirag_after0fb_g_connector_`), `MULTIRAG_CONNECTOR_DETAIL_ADDRESS`,
`MULTIRAG_CONNECTOR_DETAIL_REPORT_DIR`, and `MULTIRAG_CONNECTOR_DETAIL_CONFIG`.
Register the database, listener, config and report directory before running.
The config must contain a test signing key of at least 32 characters. Each case
appends a unique JSONL journal; the caller owns database creation/removal and
independent cleanup readback. The application SQL connection is read-only, and a
separate connection compares complete fixture state after every request.
