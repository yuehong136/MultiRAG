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
