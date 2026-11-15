# MultiRAG 模型网关（Model Gateway）

> 状态：**P1 最小闭环与 P2 限额已实现；调用记录清理、rerank 与 P3 尚未实现**（见第 10 节）
> 最后核验：2026-11-15
> 维护范围：把已登记的模型以标准模型接口对外提供、API Key、调用记录与用量、限额

本目录是模型网关的权威文档入口。它不替代模型连接与目录的合同
（[MODEL_PROVIDERS](../../api/db/services/MODEL_PROVIDERS.md)）、模型删除保护
（[模型配置生命周期](../model-provider-lifecycle.md)），也不改变既有 RAG SDK 接口。
第 10 节标为未实现的内容仍是提案，其余描述当前实现。

## 0. 启用与运行

网关默认关闭。启用后有两部分：主 API 里的管理接口，和独立的数据面进程。

```bash
MULTIRAG_MODEL_GATEWAY__ENABLED=true uv run python -m api.model_gateway.server
```

数据面进程不做数据库引导：三张表由主 API 启动时的 schema 引导创建，所以升级后先启动一次主 API。
主 API 进程也要带同一个开关，管理接口才可用。配置段是 `model_gateway`
（`common/app_config.py` 的 `ModelGatewayConfig`）：监听地址默认 `127.0.0.1:9390`，
另有请求体上限和四个超时。未启用时管理路由返回 404，数据面进程拒绝启动。

最小使用路径：

1. 在模型管理里登记一个自部署模型（已有功能）。
2. `POST /api/v1/model-gateway/models` 把它发布为公开模型名。
3. `POST /api/v1/model-gateway/keys` 创建 Key，响应里的 `key` 只出现这一次。
4. 调用方把 SDK 的 `base_url` 指向 `http://<网关地址>/v1`，用这把 Key 调用。
5. `GET /api/v1/model-gateway/logs` 和 `/usage` 查看调用记录和用量。

## 1. 定位

模型网关把租户已经登记的模型，以标准模型接口提供给外部程序调用。管理者做三件事：
把模型发布为一个公开名称，创建 API Key，查看按 Key 的调用记录和用量。之后可以按 Key
或按模型加限额。

不在范围内：

- 组织、部门、应用、项目、联系人等登记对象，以及申请、审批、分发流程。
- 钱包、计价和账单。用量是调用事实，不是费用。
- 执行工具调用、改写提示词、绑定知识库或助手。这些属于既有 RAG/Agent 接口。
- 首期不做多部署路由、权重、自动重试和回退。

### 通用性准则

网关是面向所有部署方的通用能力。新增字段或对象时逐条判定，任何一条不成立就不进核心：

1. 在默认资源范围内，「发布模型 → 创建 Key → 用标准 SDK 调用 → 查到调用记录」不需要任何额外登记。
2. Key 的说明和标签为空时，授权、调用和统计行为不变。
3. 标签和请求体里的 `user` 只用于检索和归因，不参与授权、路由或限额身份。
4. 使用方如何组织人员、提出需求、分发 Key，由部署方在平台之外决定；这些信息只会以
   Key 的名称、说明、标签值出现在数据里。
5. 需要隔离时复用既有租户范围，不为了分组新增必填层级。

## 2. 现状与缺口

| 现状（代码事实） | 结论 |
|---|---|
| `APIToken`（`api/db/db_models.py`）明文存储、整租户范围，没有有效期、启停和模型范围；现有鉴权依赖（`api/utils/api_utils.py`）对登录态和令牌一视同仁，令牌可以调用包括令牌管理在内的接口 | 不作为模型调用凭据。新建独立 Key；既有 RAG SDK 令牌保持不变 |
| `api/apps/restful_apis/openai_api.py` 的 `/api/v1/openai/{chat_id}/chat/completions` 绑定聊天助手并进入检索链路，用量是本地估算 | 不是模型直连接口。保持原合同，不在其上扩展 |
| 用量只有 `TenantLLM.used_tokens` 累计值，由 `LLMBundle`（`api/db/services/llm_service.py`）写入 | 没有按请求、按 Key 的记录。新增调用记录表 |
| `core/llm` 的驱动面向 RAG：失败以 `**ERROR**` 前缀文本返回，流式只产出文本 | 不承载标准协议。网关不经过它 |
| 模型登记为 Provider → Instance → Model；`api/db/services/model_provider_service.py` 的 `resolve_config()` 按租户、模型 ID 和能力返回连接配置，并校验启用状态 | 连接和上游凭据继续只在此维护。网关只读 |
| `common/url_utils.py` 提供 `ensure_api_version()` 等地址归一化；`core/utils/redis_conn.py` 有 Redis 令牌桶脚本 | 直接复用 |
| 没有请求级限流，也没有按租户或按 Key 的配额 | 第二阶段补齐 |

## 3. 对象模型

新增三张自有表，位于 `usr_ai` schema，沿用 `t_ai_` 前缀，都带 `tenant_id`。上游部署不新建对象，
继续使用已有的 Provider / Instance / Model。

### 公开模型 `GatewayModel`（`t_ai_gateway_models`）

| 字段 | 含义 |
|---|---|
| `id`、`tenant_id` | 标识和所属租户 |
| `name` | 公开模型名，租户内唯一；即请求体里的 `model` |
| `model_id` | 已登记模型的数字 ID（`TenantLLM.id`） |
| `model_type` | `chat` / `embedding` / `rerank` |
| `status` | `active` / `inactive` |
| `description` | 可空 |
| `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 可空，该模型所有 Key 合计的限额（第 7 节） |
| `created_by`、`updated_by` | 管理操作人 |

一个公开名对应一个已登记模型。模型语义变化（例如 embedding 更换权重）用新的公开名表达，
不引入版本号机制。

### API Key `GatewayKey`（`t_ai_gateway_keys`）

| 字段 | 含义 |
|---|---|
| `id` | 公开标识（key_id），用于调用记录关联和管理接口；不是秘密 |
| `tenant_id` | 所属租户 |
| `name` | 必填，在未撤销的 Key 之间租户内唯一；撤销后名称可以再用，方便轮换 |
| `description`、`tags` | 可空。`tags` 是字符串键值，最多 16 项 |
| `secret_hash` | 整串 Key 的 SHA-256 |
| `status` | `active` / `disabled` / `revoked`；撤销不可恢复 |
| `expires_at` | 可空 |
| `allow_all_models`、`allowed_model_ids` | 模型范围。默认不允许全部；列表为空且未显式允许全部时拒绝一切调用 |
| `rpm_limit`、`tpm_limit`、`max_parallel_requests`、`token_quota`、`quota_period` | 可空，这把 Key 的限额和配额（第 7 节） |
| `last_used_at` | 低频更新，不随每个请求写库 |
| `created_by`、`updated_by` | 管理操作人，不代表调用身份 |

Key 的形态是 `sk-mrag-<key_id>-<secret>`：`key_id` 明文，用来查行；`secret` 为高熵随机串。
解析按固定长度切分，不按分隔符。明文只在创建响应里返回一次，之后只展示前缀和 `key_id`。
轮换就是新建一把 Key 并撤销旧的，历史记录按各自的 `key_id` 保留。

### 调用记录 `GatewayRequestLog`（`t_ai_gateway_request_logs`）

| 字段 | 含义 |
|---|---|
| `id` | 服务端生成的请求 ID，同时写入响应头 `x-request-id` |
| `tenant_id`、`key_id`、`key_name` | 归属；`key_name` 是调用时的快照，改名不改写历史 |
| `model`、`gateway_model_id`、`upstream_model_id`、`upstream_model_name` | 公开名和实际调用的模型 |
| `endpoint`、`stream` | 调用的端点，是否流式 |
| `status_code`、`error_type`、`error_code` | 结果；客户端中途断开单独标记 |
| `prompt_tokens`、`completion_tokens`、`total_tokens`、`usage_details` | 用量；未知为空值，不写 0 |
| `usage_source` | `reported`（上游返回）/ `estimated` / `unavailable` |
| `started_at`、`first_token_ms`、`duration_ms` | 时间和时延 |
| `end_user` | 请求体里的 `user`，可空，仅用于归因 |
| `client_ip`、`tags` | 来源地址；调用时的 Key 标签快照，可空 |

不保存请求和响应正文。索引围绕 `(tenant_id, started_at)`、`(key_id, started_at)`、
`(gateway_model_id, started_at)`。未通过 Key 认证的请求不写调用记录。

## 4. 数据面：HTTP 透传

数据面是独立的 ASGI 应用和进程，对外提供 `/v1` 下的标准接口。它不导入 `api.apps`，
入口先调用 `common.bootstrap.ensure_initialized()`。独立进程的先例是管理服务
（`admin/server/`）和 MCP 服务（`mcp/server/`）。

首期端点：

| 端点 | 行为 |
|---|---|
| `GET /v1/models` | 返回当前 Key 有权访问的公开模型，OpenAI 列表形状 |
| `POST /v1/chat/completions` | 透传，含流式 |
| `POST /v1/embeddings` | 透传 |

`/v1/rerank`、`/v1/responses`、`/v1/messages` 等按需求逐个加入：每加一个端点，是一条转发规则加一个用量解析器。

### 为什么透传而不是经过模型驱动

对同协议的上游，网关转发原始请求体，只改三处：把 `model` 换成上游模型名，把
`Authorization` 换成上游凭据，流式聊天请求注入 `stream_options.include_usage`。

- 协议细节由上游决定，网关不重新序列化，因此不会丢字段，也不需要逐个字段保证兼容。
- 完全不依赖 `core/llm`。那是跟进 RAGFlow 的同步区
  （见 [RAGFLOW_PORTING_MAP](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)），
  它的返回形态也不适合标准协议（见第 2 节）。

代价是网关需要知道每个厂商的上游地址规则（`api/model_gateway/catalog.py` 的规则表）。目前收录：

| 厂商 | 上游根地址 |
|---|---|
| `VLLM`、`OpenAI-API-Compatible`、`Xinference`、`LocalAI`、`LM-Studio` | 登记的地址经 `common/url_utils.py` 的 `ensure_api_version()` 补上版本段 |
| `OpenAI` | 登记的地址原样使用；留空时为 `https://api.openai.com/v1` |

- 上游模型名去掉登记时追加的 `___` 后缀。
- 这些规则与 `core/llm` 里对应驱动的现有行为一致，用契约测试钉住
  （`tests/unit/test_model_gateway_catalog.py`）：同一条模型配置，网关算出的地址和模型名必须与驱动一致。
- 网关不改写请求参数。驱动为某些模型做的参数裁剪（例如去掉不被支持的采样参数）不会发生在网关里；
  上游拒绝时，它的错误原样返回给调用方。
- rerank 的上游路径各厂商不一致，逐个收录。
- 发布时就校验厂商是否在收录范围内，派发前再校验一次。

不走 OpenAI 协议的厂商以后直接调用已依赖的 `litellm` SDK 做协议转换，同样不经过 `core/llm`。

### 请求流程

1. 认证 Key，得到 `key_id`、`tenant_id` 和模型范围（第 5 节）。
2. 只解析请求体里的 `model`、`stream`、`user`，其余字段交给上游校验。请求体有大小上限。
3. 按 `(tenant_id, model)` 找到有效的公开模型，检查 Key 的模型范围，再用
   `resolve_config()` 取上游地址、凭据和模型名。
4. 改写请求并转发。认证和解析用的数据库会话在此之前关闭，流式期间不持有会话或事务。
5. 把上游响应原样返回，同时读取用量。
6. 请求结束后写一行调用记录，包括失败和客户端中途断开的情况。

约定：

- **流式用量**：强制让上游返回用量尾块。客户端自己没有要求时，仅含用量的尾块由网关消费，不转发给客户端。
- **响应里的 `model`**：首期原样返回上游值（见第 11 节的未决事项）。
- **不重试、不回退**：一次请求对应一次上游调用。
- **客户端断开**：关闭上游连接，调用记录的状态码记 499、`error_type` 为 `client_closed`，用量标为 `unavailable`。
- **流中失败**：状态码已经发出，所以以一条 `data: {"error": ...}` 事件结束流；调用记录保留已发出的状态码，
  `error_type` 为 `upstream_error`。
- **超时**：连接、首响应、流空闲和总时长分别配置。

### 错误约定

错误体统一为 `{"error": {"message", "type", "param", "code"}}`，每个响应都带 `x-request-id`。

| 情况 | 状态码 | `code` |
|---|---|---|
| Key 格式错误、不存在或秘密不匹配 | 401 | `invalid_api_key` |
| Key 已停用或撤销；Key 已过期 | 401 | `api_key_disabled`；`api_key_expired`（仅在秘密校验通过后才区分原因） |
| 请求体不是 JSON 对象；缺少 `model` | 400 | `invalid_json`；`missing_model` |
| 公开模型的类型与端点不符 | 400 | `model_endpoint_mismatch` |
| Key 无权访问该模型 | 403 | `model_not_allowed` |
| 公开模型不存在或未启用 | 404 | `model_not_found` |
| 请求体超过上限 | 413 | `request_too_large` |
| 触发限流、并发上限或配额 | 429 | `rate_limit_exceeded`；`concurrency_limit_exceeded`；`insufficient_quota`（第 7 节） |
| 上游返回 401 / 403 | 502 | `upstream_auth_error`，避免调用方误以为是自己的 Key 有问题 |
| 上游返回其他 4xx | 原样返回 | 上游的错误体，保留 `Retry-After` |
| 上游 5xx；连接失败 | 502 | `upstream_error`；`upstream_unreachable` |
| 已发布的模型当前不可用（被停用、厂商不在收录范围） | 503 | `model_unavailable` |
| 上游超时 | 504 | `upstream_timeout` |

## 5. 认证与授权

- 调用方用 `Authorization: Bearer <key>`。服务端按 `key_id` 查行，用常量时间比较校验
  `secret_hash`，再检查状态和有效期。首期每个请求直接读库，撤销和范围收窄对新请求立即生效；
  进行中的流不被中断。
- `GET /v1/models` 和实际调用使用同一个授权判断函数。
- 调用方只能提交公开模型名。请求里不能指定厂商、上游地址、上游凭据或内部模型 ID。
- 三类凭据互不通用：网关 Key 只能调用数据面；既有 RAG SDK 令牌和登录态不能调用数据面；
  网关 Key 不能调用管理接口和既有 RAG 接口。
- 管理接口要求登录态。来源为 `AuthenticationSource.SDK_API_TOKEN` 的身份
  （`api/identity/principal.py`）被拒绝，并要求与模型管理（`provider_api.py`）相同的租户权限。
- `created_by` 只记录管理操作人。调用按 `key_id` 归属，不用创建者的权限替代。

## 6. 调用记录与用量

- 每个请求在结束后写一行。写入失败不影响已经返回的响应，但要记错误日志和指标。
- 用量优先取上游返回值。取不到时标 `unavailable`，数值留空；失败或被取消的请求同样可能消耗了资源。
- 网关不更新 `TenantLLM.used_tokens`。该累计值仍只由 `LLMBundle` 写入，避免两处计数。
- 查询按 Key、模型、时间范围、状态和 `end_user` 筛选。汇总首期直接分组查询；
  数据量上来之后再加按日汇总表。
- 保留天数可配置，清理任务在第二阶段提供。停用、撤销 Key 不删除它的历史记录。

## 7. 限额

限额都是可选的，留空表示不限。没有配置任何限额的请求不经过限额器。

| 维度 | 字段 | 说明 |
|---|---|---|
| 单个 Key | `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 每分钟请求数、每分钟 token 数、并发上限 |
| 单个 Key | `token_quota`、`quota_period` | token 配额，按 UTC 自然日（`day`）或自然月（`month`）重置；两个字段必须一起设置 |
| 单个公开模型 | `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 该模型所有 Key 合计，保护上游容量；多建 Key 不能绕过 |

实现在 `api/model_gateway/limits.py`：

- **准入**：请求解析出模型后、转发前检查。先看配额，再看 token 速率、请求速率和并发。
  任何一项拒绝都不消耗其他项的额度。
- **token 事后结算**：用量在调用结束时才知道，所以越线的那次调用会完成，下一次被拒绝。
  不做预留和对账。用量未知的调用不计入 token 速率和配额。
- **速率窗口**：固定的一分钟窗口。
- **配额**：进程内计数，每个周期第一次用到时从调用记录里读出已用量，所以重启后配额依然有效。
  这是软配额：并发请求和落库时机可能带来一两次调用的偏差。
- **并发**：准入时占位，调用结束时释放，包括上游失败、流中断和客户端断开。

拒绝一律返回 429，并写调用记录：

| 情况 | `type` | `code` | 响应头 |
|---|---|---|---|
| 请求速率 | `requests` | `rate_limit_exceeded` | `Retry-After`、`x-ratelimit-limit-requests`、`x-ratelimit-remaining-requests` |
| token 速率 | `tokens` | `rate_limit_exceeded` | `Retry-After`、`x-ratelimit-limit-tokens`、`x-ratelimit-remaining-tokens` |
| 并发 | `requests` | `concurrency_limit_exceeded` | `Retry-After: 1` |
| 配额用完 | `insufficient_quota` | `insufficient_quota` | 无（重置前重试没有意义） |

限制：

- **计数在数据面进程内**。启动多个数据面进程时各自计数；需要全局一致的限额时要换成共享计数（见第 11 节）。
- 推理服务本身的并发上限仍由推理服务保证；网关限制的是准入。
- 严格预算（不可超支）需要预留、幂等结算和崩溃恢复，属于单独的扩展。

## 8. 管理面

管理接口挂在主 API 下，文件为 `api/apps/restful_apis/model_gateway_api.py`，由现有的路由自动注册加载，
响应沿用主 API 的信封。handler 用 `async def` 加 `Depends(get_async_db)`。

| 路由 | 用途 |
|---|---|
| `GET /api/v1/model-gateway/candidates` | 列出已登记的模型，以及每个模型能以哪些类型发布（不能发布时为空，页面据此置灰） |
| `GET` / `POST /api/v1/model-gateway/models`，`PATCH` / `DELETE .../models/{id}` | 发布、修改、下线公开模型 |
| `GET` / `POST /api/v1/model-gateway/keys`，`PATCH .../keys/{id}`，`POST .../keys/{id}/revoke` | 创建、修改元数据和范围、启停、撤销 Key |
| `GET /api/v1/model-gateway/logs` | 查询调用记录 |
| `GET /api/v1/model-gateway/usage` | 按 Key、模型、日期或 `end_user` 汇总用量 |

目前只能发布 `chat` 和 `embedding` 两类模型；发布和重新启用时都会校验目标模型可用且厂商在收录范围内。
限额字段在创建和修改时都可以带；修改时显式传 `null` 表示取消该项限额。Key 列表里的 `quota_used`
是当前周期的已用 token（没有配额的 Key 为 `null`）。
调用记录按页查询（每页最多 100 条），用量汇总默认取最近 7 天，日期按 UTC 划分。
汇总里的 token 只累计用量已知的调用，`usage_unknown` 给出未计入的调用数。
Web 管理页面在独立 Web 仓库的 `src/pages/settings/model-gateway/`，入口是设置里的「模型网关」：
公开模型、API Key（明文只在创建后的弹窗里显示一次）、调用记录和用量四个分区。
网关未启用时，页面显示「未启用」而不是错误。

## 9. 与 RAGFlow 跟进的隔离

网关代码放在 `api/model_gateway/`：`catalog`（目录桥）、`keys`、`store`（数据面持久化）、`relay`、
`usage`、`app` 和 `server`（数据面进程）、`management` 和 `schemas`（管理操作）。
它对现有文件的改动限于下表，全部是追加：

| 文件 | 改动 |
|---|---|
| `api/db/db_models.py` | 末尾追加三张自有表。建表函数只识别这个文件命名空间里的模型类，所以表定义必须在这里 |
| `configs/alembic/versions/` | 新增一个迁移文件 |
| `common/app_config.py` | 新增 `model_gateway` 配置段 |
| `api/db/services/model_deletion_policy.py` | `collect_blocking_model_references()` 增加一类引用：有效的公开模型 |
| `api/apps/restful_apis/model_gateway_api.py` | 新文件，管理路由 |
| `pyproject.toml` | 新增 import-linter 契约；把 `api/model_gateway` 纳入 mypy 严格检查范围 |
| `tests/support/integration_suites.py` | 登记集成测试文件 |
| [模型配置生命周期](../model-provider-lifecycle.md) | 引用表增加「有效的公开模型」一行 |

不修改：`core/llm/*`、`api/db/services/llm_service.py`、`api/db/services/tenant_llm_service.py`、
`api/apps/restful_apis/openai_api.py`、`api/utils/api_utils.py`、`api/apps/__init__.py`，以及 `APIToken`。

依赖规则：

- `api.model_gateway` 不得导入 `core.llm`、`agent`、`api.apps`、`api.db.services.llm_service`、
  `api.db.joint_services`。先按直接导入约束，与仓库里其他契约的做法一致。
- 读取模型配置只通过一个模块（目录桥），它调用 `model_provider_service` 的
  `resolve_config()` 和 `models_for_owner()`。网关其余部分不接触模型相关的 ORM。

模型删除保护沿用现有语义：有效的公开模型阻止删除它引用的模型，返回 409 和引用清单；
停用的公开模型不阻止，再次启用时重新校验引用的模型是否可用。

跟进上游提交时，只有三类变更需要复核网关，其余提交与网关无关：

1. 模型配置解析的返回内容（上游地址、凭据、模型名、厂商名）。
2. 已收录厂商的地址归一化和模型名后缀约定。
3. 模型删除流程。

前两类由契约测试暴露，第三类由删除保护的集成测试暴露。

## 10. 阶段与验收

| 阶段 | 内容 | 完成证据 |
|---|---|---|
| P1 最小闭环（**已实现**） | 三张表和迁移；管理接口；数据面的 `/v1/models`、`/v1/chat/completions`、`/v1/embeddings`；调用记录；Key 的有效期和启停；模型删除保护 | 通用性准则第 1、2 条成立；真实 SDK 完成普通和流式调用，并能按 Key 查到记录 |
| P2 管控（**限额已实现**） | 限流、并发和软配额；用量汇总 | 限额按维度生效；多把 Key 不能绕过模型级上限；重启后配额仍然有效 |
| P2 余项（未实现） | 调用记录的保留与清理；rerank | 每项单独验收 |
| P3 按需 | 非 OpenAI 协议厂商；`/v1/responses`、`/v1/messages`；多部署与回退；正文日志（手动开启）；按日汇总；管理审计；严格预算 | 每项单独立项和验收 |

验证按 [AGENTS.md 的验证表](../../AGENTS.md#验证) 执行：

- **单元**：Key 的生成、解析和校验；授权判断；流式用量解析（客户端是否要求用量两种情况）；
  错误映射。上游用受控的假服务，不依赖真实模型。
- **集成**：scratch 库上的建表和迁移；删除保护返回 409；调用记录独立读回。
- **契约**：同一条模型配置，网关与 `core/llm` 驱动算出的上游地址和模型名一致。
- **消费者**：集成测试用真实的 OpenAI Python SDK 调用网关。该 SDK 的 embeddings 默认请求 base64 编码，
  已覆盖。
- **真实上游**：`tests/manual/model_gateway_live.py` 在 scratch 库里登记环境变量给出的模型，经网关做一次
  普通调用和一次流式调用，并读回调用记录。它不随测试套件运行；每接入一类新的上游，手动跑一次。

对应的测试文件：`tests/unit/test_model_gateway_*.py`（Key、用量、目录契约、数据面、管理路由）和
`tests/integration/test_model_gateway.py`（迁移两条引导路径、SQL 持久化、Key 生命周期、用量汇总、删除保护）。

通用性验收单列：说明和标签为空时照常调用和统计；改名不改写历史；标签不能改变授权和路由；
撤销 Key 后历史仍可查；全程不需要创建组织、应用或项目记录。

## 11. 未决事项

1. 响应里的 `model` 是否改写为公开名。改写流式响应需要逐块解析和重新序列化，与原样转发冲突。
2. 多个数据面进程共享限额计数（例如放到 Redis）是否需要；如果做，共享存储不可用时默认放行还是拒绝。
3. 调用记录写入失败时，是否需要「拒绝新请求」的严格模式。
4. 管理接口是否需要自动化凭据（独立于登录态的管理 Key）。
5. embeddings 的 base64 编码依赖上游支持。上游不支持时，由网关转换还是原样返回上游错误。
6. 数据面是否提供挂载到主 API 的部署方式，作为独立进程之外的选项。

## 12. 参考与取舍

以下是 2026-11-15 核对的同类实现，链接指向各项目默认分支，括号内是核对时的提交。
它们用于说明取舍，不表示这些项目策略相同，也不表示本设计复刻其存储结构。

| 取舍 | 参考 |
|---|---|
| Key 除名称外的字段全部可选；归属层级可空 | [LiteLLM `schema.prisma`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/schema.prisma)（`09ce4f50`）、[GPUStack `api_keys.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/api_keys.py)（`aba5c665`） |
| Key 为「公开标识 + 秘密」两段，只存摘要，明文只返回一次 | 同上 GPUStack；[TensorZero 认证文档](https://github.com/tensorzero/tensorzero/blob/main/docs/operations/set-up-auth-for-tensorzero.mdx) |
| 同协议上游走 HTTP 转发，强制上游返回流式用量 | [new-api `relay-router.go`](https://github.com/QuantumNous/new-api/blob/main/router/relay-router.go)（`b388140a`）、[GPUStack `routes/openai.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/routes/openai.py) |
| 调用记录在请求结束后写一行，默认不存正文 | [LiteLLM `schema.prisma`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/schema.prisma) 的 SpendLogs、[new-api `log.go`](https://github.com/QuantumNous/new-api/blob/main/model/log.go)、[GPUStack `model_usage_details.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/model_usage_details.py) |
| token 限额事后扣减 | [Higress `ai-token-ratelimit`](https://github.com/higress-group/higress/blob/main/plugins/wasm-go/extensions/ai-token-ratelimit/README_EN.md)（`0316bc90`）、[Bifrost `budget.go`](https://github.com/maximhq/bifrost/blob/dev/framework/configstore/tables/budget.go)（`a3cd2b5b`） |
| 默认资源范围内即可签发 Key；部门和成本中心用标签承载 | [OpenAI 项目 API Key](https://developers.openai.com/api/reference/resources/organization/subresources/projects/subresources/api_keys)、[百炼业务空间](https://help.aliyun.com/zh/model-studio/use-workspace)、[方舟 API Key](https://docs.volcengine.com/docs/82379/1541594) |
| 限流响应头和 `x-request-id` | [OpenAI 限流说明](https://developers.openai.com/api/docs/guides/rate-limits) |

更细的第三方源码对照不在仓库文档里维护，分层规则见
[CHN-ADR-05](../channel-program/DECISIONS.md#chn-adr-05--文档分层入库讲我们的代码本地讲别人的代码)。
