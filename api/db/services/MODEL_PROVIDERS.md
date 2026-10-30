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

A model has an integer ID, raw provider model name, explicit capability list,
nonnegative token limit and per-model tool capability. Model references exposed
to selectors are decimal strings of that ID. Callers resolving a model with
multiple capabilities must supply the capability they need. Configuration
resolution checks tenant ownership and effective provider/instance/model status
before exposing credentials, and supplies the raw model name to the driver.
Worker rebinding must retain the authorized numeric ID and capability.

`GET /api/v1/models/default` and `PATCH /api/v1/models/default` manage defaults.
The PATCH body is `{ "model_type": "chat", "model_id": 123 }`; a null ID clears
only that capability. OCR is included. Lists return `items`, `total`, `page` and
`page_size`; pages start at one and page size is limited to 100. Query keys cannot
be duplicated, and boolean query values are exactly `true` or `false`.
An explicit empty `model_ids` list excludes catalog models. Supply at least one
custom model in that case; an entirely empty selection is rejected. Omitting
`model_ids` selects that provider's catalog. Distinct custom and discovered
models can share the same typed candidate, verification and atomic creation.
Verification itself writes no registrations; creation verifies again before its
transactional write. MinerU verification performs a real generated-PDF parse,
with the probe timeout forwarded to its HTTP request. Model route IDs require canonical
positive decimal integers; malformed, overflowing and noncanonical forms fail.

Deletion checks persisted defaults and model-reference fields in datasets,
documents, dialogs, memory, canvases and versions, saved searches, skills and
execution configurations before deleting any model. Unrelated free text and
other tenants' legacy names do not count as references. An empty provider can be
deleted. A conflict leaves the whole request unchanged.

## Existing installations

Alembic revision `b8d0f2a4c6e8` adds three tables and OCR default fields and widens
the legacy endpoint column. It keeps `TenantLLM` IDs, accounting, stored names,
status and default references. Each legacy credential/endpoint group becomes a deterministic instance.
Only a boolean model-level `is_tools` wrapper is excluded from credential
identity. Different endpoints and other credential options remain distinct,
and each model retains its own tools flag and numeric ID. New imports can reuse
an equivalent unchanged instance; existing registrations and edited instances
are never rebound or merged.
Backfill is idempotent and also links models created during user initialization.
It selects unlinked legacy rows in SQL, including the tenant filter when supplied,
rather than loading all existing model IDs. Alembic records a successful revision
in the same transaction as its data changes; an applied revision is not replayed.
New legacy rows use the explicit idempotent linkage path, not a global applied flag.
Startup default repair fills only missing numeric references with nonempty stored
aliases, including OCR, in batches of 500 tenants. It preserves existing numeric
choices and original aliases, resolves ownership and capability, and leaves
unresolved defaults eligible for a later run. Ambiguity aborts the tenant repair
transaction. Other initialization stages retain their existing commit boundaries.
Deploy through the repository's normal Alembic process before serving the new API.
This document does not authorize a production migration.

New models use opaque legacy storage keys; public projections and driver calls
resolve them to the raw model name. Existing exact legacy names remain valid,
including numeric-looking aliases. A numeric reference colliding with a distinct
persisted legacy alias fails rather than selecting a different model.

Legacy model management routes remain deprecated for the active admin client.
Their mutations reject providers already linked to the instance model with HTTP
409, before probing credentials or writing. Use the provider-instance API for
those providers. Legacy read projections retain disabled entries and numeric
references without exposing opaque keys or credentials.

Downgrade deliberately refuses to delete model registrations or restore the
shorter endpoint column. Take a database backup before migration. A rollback
requires an explicit export/conversion of any newly created instances and model
references, followed by reviewed schema changes; an automatic destructive
rollback cannot preserve those identities.

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
numeric default identities commit together.

MinerU, PaddleOCR and OpenDataLoader environment configuration is imported at
startup and registration, and when a parser explicitly needs an env fallback.
The importer uses the caller's transaction, creates linked provider/instance/model
identities, preserves edited instances and disabled registrations, and is
idempotent for unchanged credentials. GET model listings perform no imports.
PDF parsers preserve complete `model@instance@provider` references; implicit
selection prefers an active numeric OCR registration and honors an applicable
numeric OCR default. Resolution still checks tenant ownership and capability.
PaddleOCR uses an OCR candidate with zero tokens and keeps its typed algorithms,
PDF job lifecycle and independent parser initialization.

VolcEngine new structured credentials use a JSON string with `api_key` and optional
`endpoint_id`. The raw key form and persisted `ark_api_key` envelopes remain
accepted by discovery, verification and runtime. Conflicting key or endpoint aliases
are rejected without echoing their values. Chat and vision use an explicit endpoint
for the provider request while retaining the registered model name and numeric ID;
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
