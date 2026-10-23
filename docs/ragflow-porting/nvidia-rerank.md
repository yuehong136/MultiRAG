# NVIDIA reranking

The Go NVIDIA driver accepts the shared `RerankConfig`: nil or `TopN=0`
returns all input scores, positive values clamp to the number of passages,
and negative values fail before HTTP. Empty input returns an allocated empty
result without HTTP.

Requests use `query.text`, `passages[].text`, `truncate=END`, and `top_n`.
Responses map `rankings[].index/logit` into indexed rerank results, sorted by
score. Logits remain unnormalized, including zero and negative values. The
retrieval wrapper restores input order. Missing/null fields, duplicate or
out-of-range indices, wrong result counts, non-finite scores, provider errors
and trailing JSON fail rather than returning partial scores.

Calls have a 30-second total budget and honor earlier caller cancellation.
Configured instance URLs and suffixes select the NIM endpoint, normally
`<base>/ranking`. Unknown regions fail. For the catalog's default NVIDIA
host only, the two catalog rerank models select their documented hosted
retrieval endpoints instead of the chat host's `/ranking`. This is a single
endpoint selection, without retrying another host on failure. Credentials
and context remain request-local; shared catalog maps are not mutated.

Endpoint references:

- [Self-hosted NIM ranking API](https://docs.nvidia.com/nim/nemo-retriever/text-reranking/1.12.0/using-reranking.html)
- [Hosted Mistral reranking API](https://docs.api.nvidia.com/nim/reference/nvidia-nv-rerankqa-mistral-4b-v3-infer)
- [Hosted Llama reranking API](https://docs.api.nvidia.com/nim/reference/nvidia-llama-3_2-nv-rerankqa-1b-v2-infer)

Controlled HTTP tests verify payloads, indexed results, failures, cancellation,
instance isolation, and hosted endpoint selection. They do not establish
live NVIDIA credentials, model availability, or hosted inference acceptance.
