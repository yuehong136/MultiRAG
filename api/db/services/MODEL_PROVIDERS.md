# Tenant model providers and instances

The Python API manages a tenant's provider, its independently configured instances,
and each instance's models through `/api/v1/providers` and `/api/v1/models`.
`provider_api.py` owns HTTP validation and request transactions;
`model_provider_service.py` uses the caller's `AsyncSession`. It never commits
inside lifecycle operations. Mutations lock the tenant before checking conflicts
or references, and the route commits once or rolls back the request.

A provider name is unique within a tenant; an instance name is unique within that
provider. Credential values are not identities. Two instances can share a key and
use different endpoints. `api_key` is a string, including an existing provider's
complete JSON credential envelope. The API preserves provider-specific fields.
Instance listings return `has_api_key`, never the credential. An omitted or blank
PATCH credential retains it; `clear_api_key: true` explicitly clears it only for
providers that allow keyless configuration. An empty region is valid.

Tongyi-Qianwen accepts `cn`, `intl`, or an empty/default region. Empty/default
selects China. Provider listings publish official regional endpoints in the
optional `url` map. A nonempty `base_url` takes precedence; unknown regions fail
before provider I/O. Native DashScope embedding, rerank, speech and video calls
use request-local endpoints and credentials. Official compatible endpoints map
to the corresponding native `/api/v1` host; custom native endpoints must end in
`/api/v1`. A failed request never retries in another region.

The model table owns its 32-character lowercase hexadecimal ID. The registered
model name stays in `model_name`, and the instance owns the credentials and
endpoint. The Python tables under `usr_ai` use the RAGFlow capability bits for
chat, embedding, speech-to-text, image-to-text, rerank, TTS and OCR. Model metadata
keeps the token limit and tool flag in `extra`, with `used_tokens` alongside it
for MultiRAG accounting. Scoped resolution checks ownership, model/instance/provider
status and the requested capability before passing credentials to a driver.

`GET /api/v1/models/default` lists configured defaults. PATCH accepts
`{"model_type":"chat","model_id":"a42aeb7fc0064670b7bfdd2d0e13d6d8"}`.
An empty model ID clears that capability. Instance settings use PUT. DELETE
on the `/instances` or instance `/models` collection takes `instances` or
`model_name` respectively, each a nonempty name array. Model status PATCH
uses the model name path within the instance and can confirm the expected
`model_id` in the body. The direct model-create POST uses `model_type` as its
capability array and `extra.is_tools` for the tool flag.

Scoped inventories keep the MultiRAG paginated response and controlled
candidate/discovery workflow. The Go permission/membership subsystem, model
groups, scoring and decision adapters are outside this Python model contract.
An explicit empty `model_ids` list excludes catalog models. Supply at least one
custom model in that case; an entirely empty selection is rejected. Omitting
`model_ids` selects that provider's catalog. Distinct custom and discovered
models can share the same typed candidate, verification and atomic creation.
Verification itself writes no registrations; creation verifies again before its
transactional write. MinerU verification performs a real generated-PDF parse,
with the probe timeout forwarded to its HTTP request.

Deletion checks persisted defaults and model-reference fields in datasets,
documents, dialogs, memory, canvases and versions, saved searches, skills and
execution configurations before deleting any model. Unrelated free text and
other tenants' legacy names do not count as references. An empty provider can be
deleted. A conflict leaves the whole request unchanged.

## Existing installations

Alembic revision `f7a9c2e4b6d8` performs an atomic PostgreSQL cutover. It
links any remaining legacy registrations, maps old integer IDs deterministically
to hexadecimal IDs, moves model status, context limits and usage, and remaps
SQL model references by their stored field or owning tenant. It handles current
memory embedding bindings whose original fingerprints match, and retains
historical evidence of model drift. It also migrates gateway settings, key
allow-lists and request logs, including references to removed registrations.
Registration order stays independent of the new IDs so shared gateway names
keep resolving to the same model. An ambiguous selector, orphaned bound field,
unverified in-flight state, or failed integrity check aborts the transaction.
Once complete, the revision drops `t_ai_tenant_llms`; new model registrations
write only to `t_ai_tenant_models`. The old model management paths are retired.

For a deployment, take and verify a restorable backup, stop writers and drain
model-bound work, including queued and claimed memory maintenance executions,
before the cutover. Unsubmitted incremental analysis plans retain their audit
hashes and need a new discovery before submission. Deploy the matching API,
workers and Web together. Existing clients carrying numeric IDs or the old route shapes
require coordinated upgrades. First reads refresh stale Redis Agent replicas
from the migrated SQL snapshot. Restore the pre-upgrade database together with
the previous binaries if recovery is needed. The migration provides no automatic
downgrade. See [database migration](../../../docs/database-migration.md).

The source baseline is [RAGFlow model and instance routes at 91c702e](https://github.com/infiniflow/ragflow/blob/91c702e86be3dcb3cabc9f6d81e485d02241bef8/internal/router/router.go#L524-L575).

## Verification boundaries

The integration suite `tests/integration/test_provider_instances.py` uses an
isolated PostgreSQL database and a controlled OpenAI-compatible HTTP server. It
covers request rollback, independent SQL readback, ownership, references,
idempotent migration, defaults, credential separation and usage accounting.
It does not prove vendor entitlement, paid-provider behavior or a production
worker run. Browser acceptance against the Python application and final combined
CI are separate checks.

## Catalog and initialization

Static declarations accept either a single `mdl_type` or a list of capabilities.
Modern catalog and discovery retain the list, explicit `is_tools: false`, and a
zero `max_tokens`. Existing tenant registrations remain authoritative for their
capabilities; changing the static inventory does not rebind or enable saved models.
The active legacy admin inventory stores a primary capability and the vision tag
in its scalar SQL columns. Catalog replacement validates every declaration and
rolls back the replacement on failure. New-user defaults read the typed catalog
and seed only configured model references, rather than every model of each
configured factory. User, tenant, membership, root folder, model linkage and
default model identities commit together.

MinerU, PaddleOCR and OpenDataLoader environment configuration is imported at
startup and registration, and when a parser explicitly needs an env fallback.
The importer uses the caller's transaction, creates linked provider/instance/model
identities, preserves edited instances and disabled registrations, and is
idempotent for unchanged credentials. GET model listings perform no imports.
PDF parsers accept the registered model ID; implicit selection prefers an
active OCR registration and honors an applicable OCR default. Resolution still checks tenant ownership and capability.
PaddleOCR uses an OCR candidate with zero tokens and keeps its typed algorithms,
PDF job lifecycle and independent parser initialization.

VolcEngine new structured credentials use a JSON string with `api_key` and optional
`endpoint_id`. The raw key form and persisted `ark_api_key` envelopes remain
accepted by discovery, verification and runtime. Conflicting key or endpoint aliases
are rejected without echoing their values. Chat and vision use an explicit endpoint
for the provider request while retaining the registered model name and ID;
embeddings use the registered model name. A blank PATCH key preserves stored
credentials and endpoint options.

## Provider websites

These links identify providers with registered local adapters. They are informational
websites; inference endpoints remain the `url`/`base_url` contract. A registered
adapter or static model declaration does not establish access to a paid model or
prove a vendor account's capabilities.

| Provider | Website |
| --- | --- |
| Anthropic | [https://www.anthropic.com](https://www.anthropic.com) |
| Astraflow | [https://astraflow.ucloud-global.com/en-us](https://astraflow.ucloud-global.com/en-us) |
| Astraflow-CN | [https://astraflow.ucloud.cn/](https://astraflow.ucloud.cn/) |
| Avian | [https://www.avian.io](https://www.avian.io) |
| Azure-OpenAI | [https://azure.microsoft.com/en-us/products/ai-services/openai-service](https://azure.microsoft.com/en-us/products/ai-services/openai-service) |
| BaiChuan | [https://www.baichuan-ai.com](https://www.baichuan-ai.com) |
| BaiduYiyan | [https://yiyan.baidu.com](https://yiyan.baidu.com) |
| Bedrock | [https://aws.amazon.com/bedrock/](https://aws.amazon.com/bedrock/) |
| Cohere | [https://cohere.com](https://cohere.com) |
| DeepSeek | [https://www.deepseek.com](https://www.deepseek.com) |
| Fish Audio | [https://fish.audio](https://fish.audio) |
| FuturMix | [https://futurmix.ai](https://futurmix.ai) |
| Gemini | [https://gemini.google.com](https://gemini.google.com) |
| Google Cloud | [https://cloud.google.com](https://cloud.google.com) |
| GPUStack | [https://gpustack.ai](https://gpustack.ai) |
| Groq | [https://groq.com](https://groq.com) |
| HuggingFace | [https://huggingface.co](https://huggingface.co) |
| Jina | [https://jina.ai](https://jina.ai) |
| LocalAI | [https://localai.io](https://localai.io) |
| LongCat | [https://longcat.chat](https://longcat.chat) |
| LM-Studio | [https://lmstudio.ai](https://lmstudio.ai) |
| MiniMax | [https://www.minimaxi.com](https://www.minimaxi.com) |
| MinerU | [https://mineru.net/](https://mineru.net/) |
| Mistral | [https://mistral.ai](https://mistral.ai) |
| ModelScope | [https://www.modelscope.cn](https://www.modelscope.cn) |
| Moonshot | [https://www.moonshot.cn](https://www.moonshot.cn) |
| NovitaAI | [https://novita.ai](https://novita.ai) |
| NVIDIA | [https://www.nvidia.com](https://www.nvidia.com) |
| Ollama | [https://ollama.com](https://ollama.com) |
| OpenAI | [https://openai.com](https://openai.com) |
| OpenRouter | [https://openrouter.ai](https://openrouter.ai) |
| Perplexity | [https://www.perplexity.ai](https://www.perplexity.ai) |
| Replicate | [https://replicate.com](https://replicate.com) |
| PPIO | [https://ppio.com](https://ppio.com) |
| SILICONFLOW | [https://siliconflow.cn](https://siliconflow.cn) |
| StepFun | [https://www.stepfun.com](https://www.stepfun.com) |
| Tencent Hunyuan | [https://hunyuan.tencent.com](https://hunyuan.tencent.com) |
| Tencent Cloud | [https://cloud.tencent.com](https://cloud.tencent.com) |
| TogetherAI | [https://www.together.ai](https://www.together.ai) |
| TokenPony | [https://tokenpony.cn](https://tokenpony.cn) |
| Tongyi-Qianwen | [https://tongyi.aliyun.com](https://tongyi.aliyun.com) |
| Upstage | [https://www.upstage.ai](https://www.upstage.ai) |
| VLLM | [https://vllm.ai](https://vllm.ai) |
| VolcEngine | [https://www.volcengine.com](https://www.volcengine.com) |
| Voyage AI | [https://www.voyageai.com](https://www.voyageai.com) |
| Xinference | [https://github.com/xorbitsai/inference](https://github.com/xorbitsai/inference) |
| XunFei Spark | [https://xinghuo.xfyun.cn](https://xinghuo.xfyun.cn) |
| xAI | [https://x.ai](https://x.ai) |
| ZHIPU-AI | [https://www.zhipuai.cn](https://www.zhipuai.cn) |
| DeepInfra | [https://deepinfra.com](https://deepinfra.com) |
| 302.AI | [https://302.ai](https://302.ai) |
| CometAPI | [https://cometapi.com](https://cometapi.com) |
| DeerAPI | [https://deerapi.com](https://deerapi.com) |
| Jiekou.AI | [https://jiekou.ai](https://jiekou.ai) |
