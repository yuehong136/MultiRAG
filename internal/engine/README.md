# Go document stores

The Go server is a parallel implementation; Python remains the production backend.
`DocEngine` in [engine.go](engine.go) defines the active storage contract.

Elasticsearch uses one `multirag_<tenant>` chunk index and a separate
`multirag_doc_meta_<tenant>` metadata index. Chunk writes always add the explicit
KB scope. `DropChunkStore` removes only that KB's rows from the shared index.
Mappings are applied when a store is created, without constructor-time cluster
index templates. Store creation adds a missing vector dimension to an existing
chunk index; metadata creation is idempotent. All query writes validate backend
counts, conflicts, failures and refresh completion.

Metadata insertion replaces a row; `UpdateMetadata` merges keys. Existing Python
physical ES IDs are preserved and duplicate logical identities fail. Metadata
key deletion takes distinct non-empty literal keys and rejects an empty selection.
Absent keys are a no-op; removing the final key deletes the row. ES performs the
key change and final-row deletion in one script with conflict retries. Callers
must authorize document/KB/tenant identities in SQL first. Disabled chunk reads
are available only through the explicit management reader interface.

Infinity uses the pinned Go SDK with per-dataset tables. Metadata JSON columns
are decoded by row with validated length prefixes. Key deletion rejects duplicate
or malformed rows before mutation and checks request cancellation between SDK
calls. The SDK does not provide per-call context cancellation or an atomic JSON
key-update primitive; this read/modify/write operation does not promise isolation
from concurrent metadata writers. Its default non-JSON metadata update path
retains the existing merge contract.

Generic Milvus chunk/metadata writes, including metadata key deletion, return an
explicit unsupported error. Skills have their own implemented storage contract.
No empty mock result establishes generic backend support.

ES result helpers return logical chunk IDs, stored field mappings, deterministic
field/tag counts and escaped keyword highlights. They preserve indexed keyword
arrays and caller inputs. An explicitly empty KB scope produces no search results;
malformed filter selections fail before I/O. Retrieval availability filtering is
separate from authenticated management reads.

See [CLI commands](../cli/README.md#dataset-chunks-and-metadata) for the protected
management routes and grammar. Tests include controlled backend failure/identity
cases and opt-in real PostgreSQL, ES and Infinity HTTP/CLI consumers. Live tests
require owned namespaces and `REQUIRE_SERVICES=1`; protocol tests do not establish
production or hosted-provider availability.
