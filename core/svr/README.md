# Task executor boundaries

Run the Python worker with `python -m core.svr.task_executor -i 1`.
`--index` is equivalent to `-i`; the default index is `0`. The container
entrypoint supplies a unique host/index identity. The consumer name remains
`task_executor_<index>`, and the existing metrics port calculation receives
the raw index. `-t common` selects the shared task queue; other task types
are rejected because the queue does not implement separate type partitions.

`task_executor_limiter.py` owns loop-local task, parser, embedding, storage
and graph concurrency limits. Tokenizer and image helpers import those limits
directly, without importing the worker and its parser/model initialization.

The worker uses one execution path. Source writes, metadata, accounting and
pipeline history retain the current document-task and availability guards.
TOC generation owns its thread pool; the worker drains that work before
terminal bookkeeping and queue acknowledgement, including cancellation and
exceptions. Progress logs accept an absent initial Redis value. TOC model
results accept object entries or level/title pairs and retain their original
source positions when malformed entries are discarded.

Task timing and queue metrics are provided by `executor_metrics.py`; there
is no production replay path or retained chunk/vector comparison snapshot.

Worker, memory extraction/enqueue and tokenizer operations own newly created
LLM bundles through `llm_bundle_scope`. Nested operations close their own
bundles; the TOC thread creates its scope on its own event loop. Cleanup waits
for in-flight embedding/chat calls and drains repeated cancellation before
closing model-owned synchronous and asynchronous clients. Parallel embedding
calls retain their concurrency. Provider-specific `aclose`/`close` methods
take precedence over the standard `client`/`async_client` fallback. Cleanup
failures fail a successful operation; an existing business failure remains
primary and cleanup errors are logged. Caller-owned SQL sessions and the
Langfuse SDK's shared resource manager retain their existing ownership.
Parser-specific model construction inside parser threads remains the parser's
responsibility.
