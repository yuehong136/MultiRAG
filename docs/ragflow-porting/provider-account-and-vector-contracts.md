# Go provider account and vector contracts

These contracts describe the parallel Go implementation. The active Python backend is separate.

## SiliconFlow balance

`SHOW BALANCE` uses the configured `user/info` endpoint with instance credentials. A successful response requires HTTP 200, business code 0, status true and a finite string amount. `totalBalance` takes precedence over `balance`; zero remains valid. The response is `{balance, currency: "CNY"}`. Missing or malformed amounts and provider errors return an error without including the provider body or credentials.

Requests honor cancellation and the existing non-streaming call timeout. Empty regions use the default; unknown regions at the driver boundary fail explicitly. Instance binding resolves any catalog fallback before creating the private driver.

Controlled HTTP tests cover response contracts and instance URL/key isolation; they do not establish real cloud account availability.

## DeepSeek balance

`user/balance` returns the first currency in the common `balance/currency` fields and retains `is_available` plus every entry in `balance_infos`. Amounts are finite numbers; currencies remain separate. Availability false is a valid account state, including zero balances, rather than a transport failure. Missing availability, currencies, total amounts or balance entries fail explicitly. Optional granted and topped-up amounts are validated when present.

## Gitee rerank

`BAAI/bge-reranker-v2-m3` is available through the configured rerank endpoint. Requests send the query and documents with `top_n` equal to the input count and `return_documents: false`. Responses must contain exactly one finite score per document with a distinct, explicit index. Results are restored to input order, including a valid zero score. Missing or duplicate indices, missing scores, incomplete results and provider errors fail instead of supplying zero-filled results. Empty input returns an empty slice without HTTP. Requests honor the caller context and the non-streaming timeout.

## Aliyun embeddings

`text-embedding-v3` and `text-embedding-v4` use `compatible-mode/v1/embeddings` for the selected region. The driver shares the existing canonical embedding path: array input, batches of at most 32, explicit unique result indices, input-order output, nonempty finite vectors and one consistent dimension across batches. Empty input makes no HTTP call. The implementation preserves existing chat/discovery behavior and explicit unknown-region errors. Provider HTTP and business errors return no partial vectors; caller cancellation propagates.
