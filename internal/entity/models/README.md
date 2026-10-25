# Go model adapters

The Go implementation is a parallel consumer; Python remains the production backend.
Provider catalogs live in `configs/models/`. A model's declared type and its driver's
optional media interface must both match before the service invokes a provider.

| Provider | Added capabilities | Wire contract |
| --- | --- | --- |
| Astraflow | Embedding, rerank, speech | Indexed OpenAI embeddings, `results` rerank, binary speech with `format` |
| Groq | ASR, speech, model-specific reasoning options | Multipart ASR, binary speech with `response_format`; GPT-OSS `include_reasoning` and optional effort, Qwen/DeepSeek parsed reasoning |
| OpenAI | ASR and speech, including streaming | Multipart ASR; JSON/text/SRT/VTT transcription; binary or `speech.audio.*` SSE speech |
| HunYuan | Chat, reasoning, discovery, embedding | OpenAI-compatible chat/list/embedding; required key and explicit region |
| TokenHub | Chat, reasoning, discovery, embedding | OpenAI-compatible calls; bodyless model-list GET; explicit zero options retained |
| PaddleOCR_Local | OCR from bytes | No required key; base64 `file`, PDF `fileType=0`, `layoutParsingResults[].markdown.text` |
| ZHIPU-AI | OCR | `layout_parsing` accepts a data URL or provider-forwarded URL and returns `md_results` |

Local PaddleOCR uses its own provider identity and a tenant instance's explicit
base URL. It has no URL OCR interface and does not fetch local files or remote
URLs. Existing `PaddleOCR`, `MinerU` and `mineru_local` identities retain their
cloud/local semantics; changing those names would reinterpret stored instances.
The local provider must be admitted by catalog and service local-provider checks.

Media adapters receive authorized bytes from the service, never arbitrary server
file paths. Identity, credentials and endpoints cannot be overridden through media
params. Requests use caller cancellation and bounded timeouts; response sizes are
bounded. Binary speech preserves bytes and format, and propagates sender failures.
OpenAI SSE requires valid complete frames, a provider terminal and at least one
result; malformed, missing, truncated or empty streams return errors. An explicit
empty JSON transcription remains a valid silent transcription; an explicit empty
OCR page remains a valid blank page.

Embedding adapters validate the raw response before reconstruction: count,
present/unique/ranged indices, dimensions and finite non-null values. Batches
return no partial vectors after a later failure. Empty input makes no HTTP call.
Rerank uses the local all-documents meaning of `top_n=0`, and retains zero scores.

The controlled HTTP tests in `afterf9_*_test.go` cover requests, malformed raw
responses, stream termination, sender failure, deadlines and concurrent instance /
region isolation. `MULTIRAG_MODEL_TEST_RESOURCES`, when set, records each new test
listener's allocation intent, exact address and closed-port readback. These tests
require no supplier account and do not establish real supplier compatibility.

| Provider | Supported surface | Limits |
| --- | --- | --- |
| Ollama | Native `api/chat`, `api/tags`, batched `api/embed` | Installed-model catalog; optional key; explicitly configured OpenAI embedding endpoints remain supported |
| FuturMix | OpenAI chat and event-framed SSE, including reasoning deltas | Static catalog; dynamic discovery, credential probes, embedding and rerank are unsupported |
| DeepInfra | Native rerank at `v1/inference/<model>` | All raw scores must be present, finite and non-null before stable TopN selection; returned indices identify the original inputs |
| Bedrock | Text Converse, Converse-Stream and signed foundation-model discovery | No tools, vision, reasoning history, embedding, rerank or media in this adapter |

Bedrock stores credential JSON in the instance API key. `auth_mode` accepts
`access_key_secret` (`bedrock_ak`, `bedrock_sk`, optional
`bedrock_session_token`), `iam_role` (`aws_role_arn`, optional `aws_external_id`
and `role_session_name`, using the AWS credential chain or an explicit source
key pair), or `assume_role` (the default AWS credential chain, matching the
existing Python mode name). `bedrock_region` supplies the region when the
instance region is `default`; an explicit AWS instance region overrides it.
The driver synthesizes regional AWS endpoints when no base URL is supplied.
An explicit instance endpoint routes both runtime and discovery there, keeping
custom instances isolated. Direct driver configuration may use a separate
`control:<region>` endpoint. Both planes sign using service name `bedrock`.

Bedrock streams require an assistant start, indexed text blocks and block stops,
a successful `messageStop`, and clean stream completion. Provider exceptions,
malformed frames, missing output and sender errors do not emit `[DONE]`.
Streaming adapters use request cancellation and idle deadlines rather than a
total completion deadline. Tests use deterministic credentials and controlled
STS/SigV4/eventstream endpoints. They make no real AWS or IMDS requests and do
not establish production IAM permissions or paid-provider compatibility.
