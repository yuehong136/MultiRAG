# GraphRAG execution

`general/index.py` runs document build, merge, entity resolution and community
extraction under one renewable tenant/dataset writer lease. The worker reads
`parser_config.graphrag`; the API and persisted-task replay share strict numeric
validation in `common/graphrag_config.py`. Omitted execution fields use defaults.
Explicit nulls, booleans, numeric strings, fractional integers and non-finite
values are rejected for numeric settings.

| Setting | Default | Meaning |
| --- | --- | --- |
| `retry_attempts` | 2 | Total attempts for each phase, from 1 to 10 |
| `build_subgraph_retry_attempts`, `merge_retry_attempts`, `resolution_retry_attempts`, `community_retry_attempts` | omitted | Override the shared attempt count for a phase |
| `retry_backoff_seconds` / `retry_backoff_max_seconds` | 2 / 60 | Exponential delay and cap; zero permits immediate retry |
| `build_subgraph_timeout_per_chunk_seconds` / `build_subgraph_min_timeout_seconds` | 300 / 600 | Build timeout is the larger of the minimum and chunk count times per-chunk budget |
| `merge_timeout_seconds` | 180 | Per-attempt merge timeout; zero disables it |
| `resolution_timeout_seconds` / `community_timeout_seconds` | 1800 / 1800 | Per-attempt phase timeout; zero disables it |
| `lock_acquire_timeout_seconds` | 600 | Writer acquisition budget; zero selects the bounded 600-second default |

A timeout cancels and drains that attempt before another starts. Running storage and synchronous embedding
threads settle before the lease is released; consequently a hung executor call
can exceed a phase timeout while draining. Task cancellation interrupts active
attempts, lock waiting and retry backoff. Cancellation and lost leases cannot
publish completion markers.

Build retries reload checked document subgraphs. Merge retries inspect the
persisted graph and settle publication/cleanup without merging a document twice.
Resolution and community attempts receive independent deep copies, preventing a
failed attempt from contaminating the next. Phase markers are hints tied to the
successfully persisted graph/config revision; a failed phase remains retryable.
Subgraph and full-phase recovery are supported; successful individual LLM calls
inside an unfinished phase are not durable substage checkpoints. Existing model
cache behavior remains in place. NER consumes original string chunks without
LLM batching. Extraction errors propagate rather than publishing a partial
successful document graph.

Controlled integration tests use real scratch SQL, Redis and Milvus, replace
only model/fault boundaries, and independently read storage and cleanup state:
`tests/integration/test_graphrag_execution_retry.py` and
`tests/integration/test_graphrag_checkpoint_lifecycle.py`.
