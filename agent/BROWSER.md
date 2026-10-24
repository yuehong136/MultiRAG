# Browser component

`Browser` runs browser-use 0.11 with a tenant-registered model through its
`ChatOpenAI` adapter. The endpoint and credential must support OpenAI chat
completions. Provider-specific payloads and the core LLMBundle tool decorator
are separate contracts. This adapter does not update LLMBundle's tenant token
accounting. Model registration alone does not prove provider compatibility.

The node uses existing agent save, run, upload and component-debug endpoints.
Required parameters are `llm_id` and a nonempty string `prompts`. Prompts support
variable templates; component debug values override their matching references
and are recorded as the actual node inputs. `max_steps` defaults to 30 and must be a positive integer.
`headless=true`, `enable_default_extensions=false`, `chromium_sandbox=false`, and
`persist_session=true` are strict booleans. `temperature=0` is valid. The sandbox
setting must suit the deployment's browser/container security policy.

`upload_sources` is an array of variable references, persistent file IDs,
owner-bound runtime upload descriptors, or HTTP(S) URLs. A persistent file must
belong to both the execution tenant and authenticated artifact owner. Runtime
metadata is restored from the server descriptor in that owner's namespace;
client names and owner fields confer no authority. Missing, foreign and revoked
uploads fail before payload consumption. `downloaded_files` objects can be
passed to another Browser node as upload references.

URL-sourced uploads validate all redirect targets and DNS answers, pin the
connection, disable ambient proxies/netrc, bound response bytes, check a 120 second overall fetch deadline on partial
reads (with at most the socket read timeout for a stalled peer), and sanitize
filenames after decoding. These protections apply to the upload fetcher.
General browser navigation requires deployment network egress controls and is
not isolated by this guard.

Outputs are `content` (the explicit browser-use final result) and
`downloaded_files` (`[{file_id,name,size,parent_id}]`). Downloads use the actor's
root folder within the tenant, unique storage keys, an atomic SQL batch, strict
object readback and compensation on SQL/storage failure. A lost commit reply
triggers independent SQL cleanup/readback before object removal. Unconfirmed
cleanup fails explicitly; it never becomes partial success.

Every run owns a fresh browser, upload/download directories, agent workspace,
and HTTP client. Cancellation joins outstanding file IO and closes Chromium
before removing temporary files. No user browser/profile is attached. Persistent
profiles use a hash of tenant, artifact owner, graph and node; a cross-process
file lock serializes reuse. `MULTIRAG_BROWSER_PROFILE_ROOT` selects the managed
profile root. `clear_persistent_profile()` explicitly clears that node/actor
profile while holding the lock. Ordinary node reset retains the session.
The `browser-use-user-data-dir-` basename prevents browser-use 0.11.13 from
silently copying Chrome profiles into disposable directories.

Preinstall Chromium and set `BROWSER_USE_EXECUTABLE_PATH` when autodetection is
unsuitable. There are no per-run mutations of process environment variables.
Operators can disable third-party telemetry/cloud sync before process startup
with `ANONYMIZED_TELEMETRY=false` and `BROWSER_USE_CLOUD_SYNC=false`. Keep profile
and SDK config directories private and include them in operational retention
and deletion policy.

Validation:

```sh
uv run --no-sync pytest tests/unit/test_browser_component.py
make integration TESTS=tests/integration/test_browser_component_files.py
# Opt-in real Chromium with an owned loopback model and profile root:
ANONYMIZED_TELEMETRY=false BROWSER_USE_CLOUD_SYNC=false \
  BROWSER_USE_EXECUTABLE_PATH=/path/to/chromium \
  MULTIRAG_BROWSER_EVIDENCE=/absolute/owned/acceptance-directory \
  PYTHONPATH=. uv run --no-sync python tests/manual/browser_component_acceptance.py
```

The manual script tests real upload/download, same-actor session persistence,
cross-actor isolation, cancellation, profile deletion and process/listener
readback. Its model replies are controlled HTTP fixtures, not a live provider
acceptance. The integration test uses real isolated SQL and MinIO resources.
