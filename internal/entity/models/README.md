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
| HuaweiCloud | Chat/vision, discovery, embedding and rerank | DeepSeek-V3 uses V1/lowercase wire ID; other models use V2 and their declared thinking shape; full indexed vectors/scores validated |
| ModelScope | Chat and discovery | Tenant endpoint required; root or `/v1` accepted; deployment auth optional, reasoning/thinking aliases supported |
| OrcaRouter | Chat/discovery and binary speech | `v1` routes; explicit unsupported streaming speech and other unimplemented capabilities |
| Qiniu | Chat/vision and discovery | Model-specific thinking parameters; embedding, rerank and media remain unavailable |
| PaddleOCR_Local | OCR from bytes | No required key; base64 `file`, PDF `fileType=0`, `layoutParsingResults[].markdown.text` |
| 302.AI | Chat/discovery, embedding/rerank, ASR, OCR and URL parse tasks | Trimmed input identities; method-selected chat mode; bodyless authenticated inventory; authorized audio/document bytes and strict indexed results |
| Voyage AI | Embedding and rerank | `output_dimension`, `top_k`, strict raw indices and scores; no quota-consuming discovery probe |
| LongCat | Chat and authenticated discovery | `openai/v1/chat/completions`, bodyless `openai/v1/models` GET; strict bounded lists |
| xAI | Chat/discovery, ASR and binary speech | Multipart `stt`; JSON `tts` with `text`, `voice_id`, `output_format.codec`; binary sender streaming, no provider SSE assumption |
| Replicate | Chat/embedding predictions and bge rerank | Versioned `v1/predictions` with a 64-character version; JSON pair-list string `input_list`, async polling and finite raw scores |
| Gitee | Chat, embedding, rerank, OCR and parse | Default `api.moark.ai`, `china` uses `api.moark.com`, `deprecated` uses `ai.gitee.com`; custom instance URL takes precedence |
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

Replicate rerank validates the full score count before stable score ordering and
TopN selection. The bound consumer reconstructs original document order. Zero
scores are valid; `top_n=0` means all documents. Prediction cancellation, failed
or unknown terminal states, malformed output and cross-origin polling fail.
Hosted OCR remains `MinerU` / `PaddleOCR`; local OCR and parsing remain
`mineru_local` / `PaddleOCR_Local`. Catalog loading selects by provider name.

`provider_fixes_test.go` supports `MULTIRAG_PROVIDER_TEST_JOURNAL` for append-only
case identities, listener allocation intent, exact address and independent closed
port readback. `TestProviderFixesHTTPCLIAndSQL` uses an explicitly owned PostgreSQL
configuration and a freshly linked CLI; its opt-in environment is declared in the
test. Controlled provider fixtures do not establish paid-provider compatibility.

ModelScope is admitted through catalog loading, custom-model listing and SQL instance creation.
An empty key is permitted only by its explicit local-deployment policy; a tenant endpoint is
still required. The new adapters share strict raw chat/list/SSE validation, request cancellation
and idle deadlines. Huawei rerank validates all returned scores before TopN selection; the
bound facade restores input order. Controlled HTTP/JWT/SQL/CLI evidence is distinct from
real supplier credentials and production validation.

302.AI trims model names, rerank queries, document URLs and task IDs on copies;
blank required identities fail before HTTP. Chat methods determine `stream` even
when the option disagrees. Invalid stream choices or missing deltas fail even
after text has arrived; successful terminal frames may omit their delta.
JSON chat/discovery and multipart ASR request JSON
responses; sender chat requests SSE. URL parsing requires a selected model and a
public HTTP(S) URL without userinfo or fragments; task IDs use the local bounded
alphanumeric/hyphen/underscore grammar. File-path access stays in the service
boundary: model adapters receive bytes. Empty vector/rerank input makes no request,
and zero scores/options remain valid.

LongCat and Voyage use process-level shared pools with request-level credentials
and copied endpoint maps. Constructors deliberately retain those startup pools
when `http.DefaultTransport` changes later. Standard pools are cloned without
mutating the default; chat preserves custom RoundTripper routing, while the
provider pool uses a standard fallback with dial/TLS/header deadlines. This avoids
a pool per request-scoped driver. 302.AI and LongCat sender streams have a
60-second idle deadline, reset on received bytes, rather than a total stream
duration limit. Non-streaming requests retain the 120-second call deadline.
Controlled tests cover custom/nil defaults and
real loopback requests, without asserting paid supplier or production behavior.
