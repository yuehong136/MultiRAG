# REST HTTP examples

These examples target the Python/FastAPI backend at `http://127.0.0.1:8123`.
They use the registered `/api/v1` routes. The Python programs use the existing
`requests` dependency; they are HTTP client examples, not an installable SDK.
This checkout does not ship `ragflow_sdk`. Server modules under `api/apps/sdk`
are not a client library. An SDK in another checkout is not an input here.

Set an API key belonging to your test tenant without putting it in source or shell
history (for example, load it from your local secret manager). Do not enable shell
tracing. `MULTIRAG_API_KEY` is required; there is no example token fallback.

```bash
export MULTIRAG_BASE_URL=http://127.0.0.1:8123
# Set MULTIRAG_API_KEY in the environment before continuing.
bash examples/http/chat_assistant_example.sh
uv run --no-sync python examples/python/chat_assistant_example.py
```

Both commands list chats by default. To create an assistant and a session, make
non-streaming and SSE completions, list sessions and clean up, explicitly set
`MULTIRAG_ALLOW_WRITE=1`. The tenant needs a working default chat model. Completion
uses `/chat/completions` with `messages`, `chat_id` and `session_id`; the final
successful SSE frame contains `data: true`. The examples validate HTTP status and
business `code` (successful mutations may omit `data`), including streamed errors and missing stream terminators.

For chunks, point at a disposable document in a dataset owned by that tenant:

```bash
export MULTIRAG_DATASET_ID=your-test-dataset-id
export MULTIRAG_DOCUMENT_ID=your-test-document-id
bash examples/http/chunk_example.sh
uv run --no-sync python examples/python/chunk_example.py
```

These list existing chunks. With `MULTIRAG_ALLOW_WRITE=1`, they add a uniquely
named text chunk, update it with `PATCH`, read it back, then delete its exact ID.
A configured embedding model is required for manual mutations. Existing chunks
are never updated or deleted. Dataset and document creation, parsing, and worker
polling are deliberately outside these examples: use an already prepared test
document, avoiding an implicit destructive setup/teardown lifecycle.

Retrieval reads a populated dataset with semantic and keyword options:

```bash
bash examples/http/retrieval_example.sh
uv run --no-sync python examples/python/retrieval_example.py
```

Retrieval requires an API key (a browser JWT is insufficient for this route) and
configured embedding/chat models for its search options. It creates no resources;
an empty dataset may yield no results. Inspect `data.chunks`, including source
and similarity fields in the printed response.

Each request has connection/read timeouts; `MULTIRAG_TIMEOUT_SECONDS` sets a
bounded timeout in (0, 300] seconds (default 30). cURL uses it as a total limit and
Python SSE checks it as a deadline between frames. A stalled Python
stream is additionally bounded by its 15-second read timeout. Write examples use
`finally`/exit traps, including failure and normal termination, and clean only IDs
returned by their own create requests. SIGKILL or a create request whose response
is lost cannot be cleaned automatically: check the server audit logs and exact
`example-<uuid>` name before manual recovery. Cleanup failures exit nonzero and
remain visible. No example creates/deletes a dataset or document or defaults to
writing to a user's environment.

Requirements: Bash, cURL with `--fail-with-body`, jq and Python 3; Python examples
use the repository's installed `requests`. Local contract validation uses actual
HTTP routes, scratch SQL and native search storage with controlled model
boundaries; it does not certify a real provider, worker parsing or production.
