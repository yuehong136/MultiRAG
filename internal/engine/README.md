# Go document engines

Python remains the production backend. Go uses the original `DocEngine` contract in
[engine.go](engine.go), with Elasticsearch and Infinity implementations in their
corresponding directories. No Milvus implementation or management-reader adapter
is retained outside the frozen target.

ES stores chunks in a shared `multirag_<tenant>` index and metadata in
`multirag_doc_meta_<tenant>`. Chunk writes bind the explicit dataset; typed ID
conditions are retained, invalid predicates fail before I/O, and query-write
conflicts, partial failures and timeouts are errors. Physical chunk IDs include
the dataset to avoid cross-KB collisions. `DropChunkStore` deletes only the given
KB rows in a shared tenant index. The original dedicated Skills index uses
dataset `skill`; its drop validates the index name and requires acknowledged
physical deletion. Chunk-store creation installs the actual requested vector
dimension, including a new dimension added to an existing tenant index.
Single updates wait for refresh. Metadata uses the original query-based
merge/delete route, preserving literal JSON term identities. Removing the final
metadata key deletes its row. These multi-request metadata operations do not
promise atomicity against concurrent writers.

`GetChunk` is the frozen scoped direct read; retrieval availability is selected
by the caller's Search filter. There is no additional management-read interface.
Search keeps the original ordered text/dense/fusion route and field/sort shapes.
Explicit empty dataset scope returns no rows. Backend failures are errors;
metadata JSON retains integer precision. Highlight output escapes source HTML
and retains only the highlighting `<em>` markup.

Infinity uses the pinned SDK and per-dataset tables. Original filter builders
handle quoted IDs without silently changing their values. Packed metadata JSON
is validated and aligned by the existing metadata functions. The SDK lacks
per-call context cancellation; cancellation before a call is checked, but an
in-flight SDK operation is not claimed to be interruptible. No pooled-client or
new query architecture is introduced. Metadata read/modify/write is not atomic.

Redis initialization can retry after failure and close; Health uses a bounded
read-only PING. NATS uses configured address/subject, validates task frames and
closes its connection during engine shutdown. Real ES/Infinity/Redis/NATS tests
use isolated resources with independent cleanup checks. See the
[development guide](../../docs/development.md#go-并行实现的本地验证).
