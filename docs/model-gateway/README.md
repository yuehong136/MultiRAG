# MultiRAG 模型网关（Model Gateway）

> 状态：**P1 最小闭环、P2 限额、模型目录（取代发布）、调用记录清理、费用与按日汇总、多部署路由、Redis 共享计数、后台健康检查、Key 轮换、`/v1/messages` 已实现；rerank 与 P3 余项尚未实现**（见第 10 节）
> 最后核验：2026-11-25
> 维护范围：把已登记的模型以标准模型接口对外提供、API Key、调用记录与用量、限额

本目录是模型网关的权威文档入口。它不替代模型连接与目录的合同
（[MODEL_PROVIDERS](../../api/db/services/MODEL_PROVIDERS.md)）、模型删除保护
（[模型配置生命周期](../model-provider-lifecycle.md)），也不改变既有 RAG SDK 接口。
第 10 节标为未实现的内容仍是提案，其余描述当前实现。

## 0. 启用与运行

仓库的 `configs/service_conf.yaml` 默认启用网关。主 API 启动后提供管理接口，数据面进程单独启动。

```bash
uv run python -m api.model_gateway.server
```

数据面进程不做数据库引导：网关的表由主 API 启动时的 schema 引导和迁移创建，所以升级后先启动一次主 API。
使用自定义配置文件时，主 API 和数据面进程均需启用 `model_gateway.enabled`。配置段是 `model_gateway`
（`common/app_config.py` 的 `ModelGatewayConfig`）：监听地址默认 `127.0.0.1:9390`，
`public_base_url` 是调用方使用的地址（显示在管理页面上，留空时由监听地址推导），
另有请求体上限、四个超时、多部署的重试次数和冷却时间（`num_retries` 默认 2、`cooldown_time_seconds` 默认 5，见第 4 节）
和调用记录保留天数（`log_retention_days`，默认 0 即不清理，见第 6 节）。
`state_store` 决定限额计数和冷却状态放在 Redis（`redis`，默认）还是本进程（`memory`），见第 7 节；
`health_check_interval_seconds` 开启后台健康检查，见第 4 节；`price_map_refresh_hours` 和 `price_map_url` 控制价格表的定时拉取，见第 6 节。
这两项默认开启，分别是 300 秒和 24 小时，设为 0 关闭；不能访问外网的环境把 `price_map_refresh_hours` 设为 0 或改 `price_map_url`。
注意本地覆盖文件按顶层配置段整体替换：`local.service_conf.yaml` 里只写 `enabled` 时，其余各项取的是这些代码默认值；`messages_api_warm_up`（默认开）让数据面启动时预先加载 `/v1/messages` 的转换器。未启用时管理路由返回 404，数据面进程拒绝启动。

最小使用路径：

1. 在模型管理里登记模型（已有功能）。它会自动出现在网关目录里，不需要发布。
2. `POST /api/v1/model-gateway/keys` 创建 Key 并选择可调用范围，响应里的 `key` 只出现这一次。
3. 调用方把 SDK 的 `base_url` 指向 `http://<网关地址>/v1`，用这把 Key 和模型名调用。
4. `GET /api/v1/model-gateway/overview`、`/logs` 和 `/usage` 查看用量和调用记录。

## 1. 定位

模型网关把租户已经登记的模型，以标准模型接口提供给外部程序调用。已连接提供商里的模型
自动进入网关目录，管理者主要做两件事：创建 API Key 并选择它能调用的提供商连接或模型，
查看调用记录和用量。需要时可以关闭某个模型、给模型起别名，或按 Key、按模型加限额。

不设单独的发布步骤：同类网关（New-API、One-API、LiteLLM、Bifrost、Portkey）都直接把已配置的
模型作为可授权对象，暴露范围由每把 Key 的授权和模型上的开放开关决定（第 12 节）。

不在范围内：

- 组织、部门、应用、项目、联系人等登记对象，以及申请、审批、分发流程。
- 钱包、充值和账单。费用是按价格表折算的参考值（美元），不扣款，也不参与限额。
- 执行工具调用、改写提示词、绑定知识库或助手。这些属于既有 RAG/Agent 接口。
- 不对同一个部署自动重试；不做按延迟、按用量的路由策略。多部署路由、冷却和换部署重试见第 4 节。

### 通用性准则

网关是面向所有部署方的通用能力。新增字段或对象时逐条判定，任何一条不成立就不进核心：

1. 在默认资源范围内，「登记模型 → 创建 Key → 用标准 SDK 调用 → 查到调用记录」不需要任何额外登记。
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
| 用量只有 `TenantModel.used_tokens` 累计值，由 `LLMBundle`（`api/db/services/llm_service.py`）写入 | 没有按请求、按 Key 的记录。新增调用记录表 |
| `core/llm` 的驱动面向 RAG：失败以 `**ERROR**` 前缀文本返回，流式只产出文本 | 不承载标准协议。网关不经过它 |
| 模型登记为 Provider → Instance → Model；`api/db/services/model_provider_service.py` 的 `resolve_config()` 按租户、模型 ID 和能力返回连接配置，并校验启用状态 | 连接和上游凭据继续只在此维护。网关只读 |
| `common/url_utils.py` 提供 `ensure_api_version()` 等地址归一化 | 直接复用 |
| `core/utils/redis_conn.py` 有 Redis 令牌桶脚本 | 没有用它：那是同步客户端和令牌桶，网关的限额是固定一分钟窗口，用自己的 Lua 脚本和异步客户端（第 7 节） |
| 没有请求级限流，也没有按租户或按 Key 的配额 | 第二阶段补齐 |

## 3. 对象模型

网关自有五张表，位于 `usr_ai` schema，沿用 `t_ai_` 前缀，都带 `tenant_id`。上游部署不新建对象，
继续使用已有的 Provider / Instance（连接）/ Model。

### 模型目录（不落表）

目录是租户全部已登记模型的派生视图（`api/model_gateway/directory.py`），由三部分合成：
`catalog` 读出的登记信息、下面的模型设置行、租户设置。每个模型有：

| 属性 | 含义 |
|---|---|
| `servable_types` | 网关有端点的能力，目前是 `chat`、`embedding` 的子集 |
| `reason` | 当前不能服务的原因：`provider_not_supported`（厂商不在转发规则里）、`endpoint_not_supported`（没有可服务的能力）、`model_disabled`（模型、连接或厂商在模型管理里停用） |
| `enabled` | 开放开关。没人设置过时取租户默认值（`enabled_by_default` 为真） |
| `state` | 有 `reason` 时为 `unavailable`；否则关闭为 `closed`，开放为 `callable` |
| `names` | 能调到它的全部名字，第一个是调用名 `call_name` |

**调用名规则**。每个模型可以被以下名字调用，字符串冲突时按此顺序优先：

1. 别名（可选；几个模型可以填同一个别名）；
2. `提供商/连接名/模型名`，始终唯一；
3. `提供商/模型名`，同一提供商内同名模型只有一个拥有它；
4. 模型名本身，同名模型只有一个拥有它。

第 3、4 条由标记为首选（`preferred`）的模型拥有，否则由最早登记的模型拥有。所有已登记模型都参与
命名，无论是否开放，所以开放、关闭或新增模型都不会抢走另一个模型已经在用的名字。模型名去掉登记时
追加的 `___` 后缀。调用名依次取别名、模型名、`提供商/模型名`、`提供商/连接名/模型名` 中第一个归它的。

**多部署**。"拥有"只决定这个名字在目录和 `/v1/models` 里列在哪个模型下。调用一个名字时，在同一级
有这个名字的模型都可以承接：同别名的模型、同提供商同模型名的模型（`提供商/模型名`）、同模型名的模型
（模型名本身）。它们是这个名字的多个部署，对应 LiteLLM 的 model group；怎么选见第 4 节。
`提供商/连接名/模型名` 始终只指一个模型。别名优先于模型名：一个别名和另一个模型的模型名相同时，
这个名字只指有别名的那个模型。

### 模型设置 `GatewayModelSetting`（`t_ai_gateway_model_settings`）

只有管理者改过某个模型时才有这一行。

| 字段 | 含义 |
|---|---|
| `id`、`tenant_id` | 标识和所属租户 |
| `model_id` | 已登记模型的 32 位十六进制字符串 ID（`TenantModel.id`）；模型删除后保留该行供追溯 |
| `enabled` | 开放开关 |
| `alias` | 可空；不唯一，同别名的模型是这个名字的多个部署。别名不能是另一个模型的非别名调用名 |
| `preferred` | 同名模型共用的名字列在哪个模型下；设为真时清除其他同名模型的标记。不影响流量分配 |
| `weight`、`deployment_order` | 可空，多部署路由用，对应 LiteLLM 的 `weight` 和 `order`（接口字段名是 `weight`、`order`）；权重为空按 1，顺序为空排在所有有顺序的部署之后 |
| `description` | 可空 |
| `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 可空，该模型所有 Key 合计的限额（第 7 节） |
| `input_cost_per_token`、`output_cost_per_token`、`cache_read_input_token_cost` | 可空，管理者给这个模型定的价格（美元 / token，字段名与 LiteLLM 一致）；为空时用 LiteLLM 价格表（第 6 节） |
| `created_by`、`updated_by` | 管理操作人 |

### 租户设置 `GatewaySetting`（`t_ai_gateway_settings`）

| 字段 | 含义 |
|---|---|
| `tenant_id` | 唯一 |
| `new_models_enabled` | 没有模型设置行的模型是否开放，默认开放。修改时，已经存在、还跟随默认值的模型写入当前状态，所以只有之后登记的模型跟随新默认值 |

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
| `allow_all_models`、`allowed_instance_ids`、`allowed_model_ids` | 可调用范围：全部开放模型（含以后开放的）；或者整个连接（含该连接以后开放的模型）加上逐个指定的已登记模型 ID（快照）。默认不允许全部；两个列表都为空且未允许全部时拒绝一切调用。ID 指向的对象删除后保留，管理页面据此显示已失效 |
| `rpm_limit`、`tpm_limit`、`max_parallel_requests`、`token_quota`、`quota_period` | 可空，这把 Key 的限额和配额（第 7 节） |
| `last_used_at` | 低频更新，不随每个请求写库 |
| `created_by`、`updated_by` | 管理操作人，不代表调用身份 |

Key 的形态是 `sk-mrag-<key_id>-<secret>`：`key_id` 明文，用来查行；`secret` 为高熵随机串。
解析按固定长度切分，不按分隔符。明文只在创建响应里返回一次，之后只展示前缀和 `key_id`。
轮换（`POST /keys/{key_id}/rotate`）给同一把 Key 换一个新密钥：`key_id`、名称、范围、限额、用量和调用记录都不变。
被换下的密钥摘要存在 `previous_secret_hash`，在 `previous_secret_expires_at` 之前仍然能通过认证，对应 LiteLLM
`/key/regenerate` 的 `grace_period`；宽限期为 0 时旧密钥立即失效。只保留最近一次被换下的密钥，再次轮换会让更早的立即失效；
撤销 Key 时一并清掉。`rotated_at` 记录最近一次轮换的时间。

### 迁移已有调用方 Key

平台管理员在 Web 的「设置 → 模型网关 → API Key → 导入已有 Key」下载 JSON 模板、选择清单、预览结果，再确认整批导入。Web 限当前管理员自己的租户，每批最多 1,000 条，文件最大 2 MiB。模板默认停用且未授权模型，请按原策略填写。网页接受下述摘要清单，也支持用 `key` 代替 `sha256`；通过 HTTPS 或 localhost 打开时，浏览器可将完整旧 Key 就地计算成摘要。导入接口只接受摘要。选定文件后请核对预览中的名称、状态和模型范围。遇到冲突时，修正清单并重新预览。

具备目标数据库运维权限的管理员也可以用 CLI 导入相同格式的摘要清单。两种入口共用冲突校验和事务写入逻辑。导入成功的记录进入所属租户的 Key 列表，沿用网关的模型范围、限额、用量、停用、撤销和轮换。管理员第一次轮换会签发标准 `sk-mrag-` 密钥，原调用方可以在设定的宽限期内完成替换。

在受控源环境里，对客户端实际发送的**完整 token**（含其前缀，去掉 HTTP 的 `Bearer `）按 UTF-8 计算 SHA-256，写入清单的 `sha256`。转换器可调用 `api.model_gateway.keys.import_lookup_digest` 验证格式并计算摘要。可导入主体为 32–256 个 Bearer 字符，末尾可另带最多两个 `=`，原生网关与 SDK 命名空间保留。默认使用 Bearer 或 `x-api-key`。

若源系统接受同一凭据的多种写法，可将已核实为同等权限的其他完整写法逐个计算摘要，放入可选的 `alternate_sha256` 列表。若源系统允许直接将 token 作为 Authorization 头的值，可在该条记录设置 `allow_raw_authorization: true`。所有写法共用一条网关记录及其额度、宽限期、停用和撤销状态；每种写法的摘要在所有租户间唯一。具有额外语义的后缀（例如管理员指定渠道）不能登记为等价写法。

下面是一份清单的结构。将示例占位值替换为真实的目标租户 ID、目录模型 ID 和 64 位小写十六进制摘要。每个来源应保持稳定的实例名和记录 ID，同名 Key 通过来源 ID 区分；同一租户内仍需满足现有名称唯一规则。

```json
{
  "source": "previous-gateway",
  "keys": [{
    "source_id": "42",
    "tenant_id": "<target tenant>",
    "name": "support-bot",
    "description": "客服应用",
    "sha256": "<digest of the complete bearer token>",
    "status": "active",
    "expires_at": null,
    "allowed_model_ids": ["<registered model id>"],
    "unresolved": []
  }]
}
```

在已升级数据库的后端环境中执行。预览只查询数据库；加 `--apply` 后，所有条目通过才会在同一事务写入。

```bash
python -m api.model_gateway.import_keys migration.json
python -m api.model_gateway.import_keys migration.json --apply
```

清单必须显式提供状态、带时区的到期时间或 `null`，以及 `unresolved`。源转换器应把未映射的账户归属、余额、子网、模型或其他授权约束列在 `unresolved` 中；非空会阻止整批写入。保留原限制的模型应映射到具体的 `allowed_model_ids`。仅在源权限确实覆盖整个连接或未来新增模型时，才使用连接范围或 `allow_all_models`。旧系统的账户余额和本系统的周期 token 配额须单独核对。

输出报告按来源 ID 列出预览、已登记及冲突条目；相同清单可重跑，管理页面随后做出的撤销、轮换和范围变更优先。源数据变动会报告冲突，需由管理员核对差异。凭据摘要跨租户唯一，撤销或轮换后仍保留来源映射以防误重新签发。历史清单和凭据摘要留在部署环境。需要回退到旧 schema 时，应使用导入前配套备份。

### 调用记录 `GatewayRequestLog`（`t_ai_gateway_request_logs`）

| 字段 | 含义 |
|---|---|
| `id` | 服务端生成的请求 ID，同时写入响应头 `x-request-id` |
| `tenant_id`、`key_id`、`key_name` | 归属；`key_name` 是调用时的快照，改名不改写历史 |
| `model`、`upstream_model_id`、`upstream_model_name` | 调用方请求的模型名和实际调用的已登记模型；`gateway_model_id` 只在目录取代发布之前的记录里有值 |
| `endpoint`、`stream` | 调用的端点，是否流式 |
| `status_code`、`error_type`、`error_code` | 结果；客户端中途断开单独标记 |
| `prompt_tokens`、`completion_tokens`、`total_tokens`、`usage_details` | 用量；未知为空值，不写 0 |
| `cache_read_input_tokens` | 命中上游缓存的输入 token（包含在 `prompt_tokens` 里）；上游没有报告时为空 |
| `cost` | 这次调用按当时价格折算的美元费用；模型没有价格或用量未知时为空，不写 0 |
| `attempts` | 这次请求换过部署时，按先后记下每一次：模型、提供商、连接名、状态码、错误码、耗时；只调用了一个部署时为空 |
| `usage_source` | `reported`（上游返回）/ `estimated` / `unavailable` |
| `started_at`、`first_token_ms`、`duration_ms` | 时间和时延 |
| `end_user` | 请求体里的 `user`，可空，仅用于归因 |
| `client_ip`、`tags` | 来源地址；调用时的 Key 标签快照，可空 |

不保存请求和响应正文。索引围绕 `(tenant_id, started_at)`、`(key_id, started_at)`、
`(gateway_model_id, started_at)`，另有 `(started_at)` 供清理使用。未通过 Key 认证的请求不写调用记录。

### 按日汇总 `GatewayDailyUsage`（`t_ai_gateway_daily_usage`）

每个「租户、UTC 日期、Key、模型、端点」一行，唯一约束就是这五列。写调用记录的同一个事务里对这一行做
`INSERT ... ON CONFLICT DO UPDATE` 累加，所以汇总和调用记录不会不一致，清理调用记录后汇总仍在。

| 字段 | 含义 |
|---|---|
| `date`、`key_id`、`model_id`、`endpoint` | 汇总维度；`model_id` 是实际调用的已登记模型，在解析出模型之前就被拒绝的调用记为空串 |
| `key_name`、`model_name` | 最近一次调用时的名称快照 |
| `prompt_tokens`、`completion_tokens`、`cache_read_input_tokens`、`total_tokens`、`spend` | 只累计用量已知的调用；`spend` 是美元 |
| `api_requests`、`successful_requests`、`failed_requests`、`usage_unknown_requests`、`total_response_time_ms` | 调用数、成功数、失败数、用量未知的调用数、总耗时 |

度量列沿用 LiteLLM 日汇总表（`LiteLLM_DailyUserSpend`）的列名。迁移 `0a2c4e6f8b1d` 用已有的调用记录回填这张表。

## 4. 数据面：HTTP 透传

数据面是独立的 ASGI 应用和进程，对外提供 `/v1` 下的标准接口。它不导入 `api.apps`，
入口先调用 `common.bootstrap.ensure_initialized()`。独立进程的先例是管理服务
（`admin/server/`）和 MCP 服务（`mcp/server/`）。

首期端点：

| 端点 | 行为 |
|---|---|
| `GET /v1/models` | 返回当前 Key 可调用的开放模型的调用名，OpenAI 列表形状 |
| `POST /v1/chat/completions` | 透传，含流式 |
| `POST /v1/embeddings` | 透传 |

`/v1/rerank`、`/v1/responses` 等按需求逐个加入：每加一个端点，是一条转发规则加一个用量解析器。

### `/v1/messages`：Anthropic 协议入口

`POST /v1/messages` 接受 Anthropic Messages 请求，由同一批对话模型应答，供只会说 Anthropic 协议的客户端
（Anthropic SDK、Claude Code 等）使用。上游仍然是 OpenAI 协议：请求转成 chat completion 发出去，响应（整段或流式）
转回 Messages 格式。请求和整段响应的转换用 LiteLLM 的适配器
（`litellm/llms/anthropic/experimental_pass_through/adapters`），`api/model_gateway/anthropic.py` 负责喂数据、清理和组帧。

2026-11-26 对照 Anthropic 官方文档、Claude Code 的网关兼容要求、LiteLLM 主干、new-api 和 Bifrost 复查了一遍，按它们的做法改了这些：

- **流式事件由 `anthropic_stream.py` 生成，不再用已安装的 LiteLLM 1.82 的 `AnthropicStreamWrapper`。** 那一版的事件队列是类级共享的
  （并发的两条流会互相收到对方的事件），切换内容块时丢掉触发块（工具名和首段参数同块到达时参数丢失），
  `reasoning_content` 被放进 text 块。LiteLLM 主干已重写这部分但不在锁定的版本里；这里的规则照主干和 new-api 的
  `to_claude_messages_resp`：同一时刻只开一个块，某类内容第一次出现时开块，每个工具调用开一个 `tool_use` 块并带上同块到达的参数，
  `reasoning_content` 进 `thinking` 块。升级 LiteLLM 后可以换回它的实现。
- **上游沉默时每 10 秒发一条 `ping`**（new-api 的 `StartStreamKeepAlive` 同样是 10 秒）。长思考时这可能是流上唯一的字节。
- **用量按 Anthropic 的口径报告**：`input_tokens` 不含缓存命中，`cache_read_input_tokens`、`cache_creation_input_tokens` 单列，流式和整段一致。
- **整段响应**：`id` 是 `msg_…`，`model` 是调用方用的名字，`content_filter` 映射为 `refusal`，不输出空的 text 块和 LiteLLM 的私有字段。
- **请求清理**：`messages` 里的 `system` 条目并入 `system`（Claude Code 对不认识的模型名会这样发）；`stop_sequences` 转成 `stop`；
  `metadata.user_id` 记为调用记录的终端用户；纯文本 `document` 转成文本，PDF 转成 chat completions 的 `file` 部件；
  OpenAI 协议上游不认识的字段（`top_k`、`thinking`、`output_config`、`context_management`、`service_tier` 等）和
  只能在 Anthropic 侧执行的托管工具（bash、web search 等）不发给上游。`thinking` 的预算已由适配器换算成 `reasoning_effort`。
- **`POST /v1/messages/count_tokens`**：用 LiteLLM 的 `token_counter` 在本地估算，返回 `{"input_tokens": n}`；校验 Key 和模型权限，
  不调用上游，不写调用记录。这是估算值，不是上游分词器的结果。
- **`GET /v1/models`** 在请求带 `anthropic-version` 头时返回 Anthropic 的分页形状（`data[].type=model`、`display_name`、`created_at`、
  `has_more`、`first_id`、`last_id`），new-api 用同样的判断。
- **错误**：错误体带 `request_id`，响应同时带 `request-id` 头（Anthropic SDK 读这个名字）；504 的类型是 `timeout_error`；
  `/v1/messages` 下未知路径的 404/405 也用 Anthropic 的信封。

- 认证：除 `Authorization: Bearer` 外也接受 `x-api-key`（三个端点都接受）。
- 选部署、换部署重试、限额、调用记录、费用与 `/v1/chat/completions` 完全相同；调用记录的 `endpoint` 是 `messages`。
- 错误用 Anthropic 的信封 `{"type":"error","error":{"type","message"}}`，`type` 按状态码取
  （401 `authentication_error`、403 `permission_error`、404 `not_found_error`、429 `rate_limit_error`、5xx `api_error` 等）；
  上游的 4xx 保留它的 `message`。流到一半失败时以一条 `error` 事件结束。
- 流式时网关总是让上游返回用量，因为 Messages 格式在最后的 `message_delta` 里报告用量。
- `litellm` 导入要几秒，所以按需加载，不在数据面导入时加载；`messages_api_warm_up` 为真时启动后在后台预先加载。
- 还没有做的：上游本身是 Anthropic 协议时的原生透传（见第 11 节）；`tool_result.is_error` 不会传给上游；
  多个工具调用的参数交错到达时，后到的那段会被丢弃并记一条警告（OpenAI 协议的上游实际按顺序发）。
- 只用受控的假上游和真实 Anthropic SDK 验证过，没有对真实上游或真实 Claude Code 跑过。

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
| `Tongyi-Qianwen` | DashScope 的 OpenAI 兼容根地址，聊天和向量都走它：留空时为 `https://dashscope.aliyuncs.com/compatible-mode/v1`；阿里云主机（`*.aliyuncs.com`）上的原生地址（裸主机或 `/api/v1`）换成同一主机的 `/compatible-mode/v1`；其他地址原样使用；其他主机上的 `/api/v1` 只支持原生协议，网关不能服务 |
| `DeepSeek`、`Moonshot`、`ZHIPU-AI`、`SILICONFLOW`、`StepFun`、`01.AI`、`BaiduYiyan`、`Tencent Hunyuan`、`PPIO`、`NovitaAI`、`GiteeAI`、`302.AI`、`Jiekou.AI`、`LongCat`、`n1n`、`Avian` | 只转发对话。登记的地址原样使用；留空时为该厂商的公开根地址，与 `core.llm.FACTORY_DEFAULT_BASE_URL` 和驱动构造函数的默认值逐个一致。这些厂商的向量驱动登记的是另一种地址形态，网关不转发其向量模型（`endpoint_not_supported`） |
| `CometAPI`、`DeerAPI`、`PerfXCloud`、`Upstage`、`Astraflow`、`Astraflow-CN`、`FuturMix` | 对话和向量都转发：向量驱动是同一根地址上的 OpenAI 协议。地址规则同上一行 |

尚未收录但驱动走 OpenAI 协议的厂商：`OpenRouter`、`MiniMax`、`VolcEngine`（登记的 Key 是带附加字段的编码，需要先解码）、
`xAI`、`Groq`、`TogetherAI`、`DeepInfra`、`NVIDIA`（驱动把地址交给 LiteLLM 内置表，本仓库没有可对照的默认值）。

- 上游模型名去掉登记时追加的 `___` 后缀。
- 这些规则与 `core/llm` 里对应驱动的现有行为一致，用契约测试钉住
  （`tests/unit/test_model_gateway_catalog.py`）：同一条模型配置，网关算出的地址和模型名必须与驱动一致。
  通义千问的驱动本身分两条路：聊天驱动在兼容根地址上走 OpenAI 协议，地址与网关逐字一致；
  向量驱动和原生地址上的聊天驱动走 DashScope 原生接口，契约测试钉住的是同一主机和同一模型名。
- 网关不改写请求参数。驱动为某些模型做的参数裁剪（例如去掉不被支持的采样参数）不会发生在网关里；
  上游拒绝时，它的错误原样返回给调用方。
- rerank 的上游路径各厂商不一致，逐个收录。
- 目录里标出不在收录范围的厂商（`provider_not_supported`），派发前再校验一次。

不走 OpenAI 协议的厂商以后直接调用已依赖的 `litellm` SDK 做协议转换，同样不经过 `core/llm`。

### 多部署：顺序、权重、冷却和换部署重试

实现在 `api/model_gateway/router.py`，规则和默认值取自 LiteLLM 的路由器
（`litellm/router_utils/cooldown_handlers.py`、`litellm/router_strategy/simple_shuffle.py`、`litellm/constants.py`），
所以 LiteLLM 的文档可以直接对照。

- **顺序（`order`）**：先用数字最小的一级，这一级失败或在冷却时才用下一级；没有设顺序的部署排在最后。
- **权重（`weight`）**：同一级里按权重随机选，默认都是 1，即均分。
- **换部署重试**：上游返回 401、403、404、408、409、429、5xx，或者连不上、等响应头超时，并且还有别的部署时，
  换下一个。一次请求最多调用 `1 + num_retries` 个部署（默认 3 个）。其他 4xx 是上游对请求本身的判定，
  原样返回，不换部署。流式请求只在状态行发出之前换，流到一半失败不换。
- **最后一个部署的结果就是请求的结果**，返回方式与单部署完全相同；调用记录、费用和按日汇总都记在它名下。
- **冷却**：部署失败后暂停 `cooldown_time_seconds` 秒（默认 5，设 0 关闭），期间排到所有健康部署之后。
  触发条件照 LiteLLM：429 立即冷却；401、404 立即冷却；其余失败在当前这一分钟失败率超过 50% 且至少 5 次调用时冷却。
  名字只有一个部署时，429 和失败率都不触发冷却，除非一分钟内 1000 次以上调用全部失败。
- **部署自己的限额**：达到模型级限额（第 7 节）的部署直接跳过，不调用上游，也不占重试次数；
  所有部署都在限额上时返回 429。Key 的限额在选部署之前检查一次。

与 LiteLLM 不同的三处：一次请求不会两次调用同一个部署，所以只有一个部署的名字不重试（LiteLLM 会对同一个部署
重试，而调用方的 SDK 通常也重试，叠加起来放大上游压力）；所有部署都在冷却时照常依次尝试，而不是直接拒绝请求
（对健康状况判断错了也不至于让一个名字整体不可用）；连不上上游按 503 计入失败率（LiteLLM 不把它自己的
`APIConnectionError` 计入，因为那个类型还包含本地错误）。

失败计数在数据面进程的内存里。`state_store: redis` 时冷却本身是共享的：一个进程让某个部署冷却，会在 Redis 里写一个
带过期时间的标记，其他进程选部署前读到它就同样让这个部署靠后（`api/model_gateway/redis_state.py` 的 `RedisCooldowns`）。
什么时候该冷却仍由各进程按自己看到的调用判断，和多个 LiteLLM 实例一样。管理面看不到某个部署是否在冷却。

### 后台健康检查

`api/model_gateway/health.py`，`health_check_interval_seconds` 大于 0 时开启，默认 300 秒（LiteLLM 的默认间隔；LiteLLM 的
`background_health_checks` 本身默认关，这里按产品要求默认开），设为 0 关闭。每一轮查清「有可用 Key 的租户」的每个可调用模型
上游能不能用，不能用时说明原因。规则取自三个项目，不是自己定的：

- **探测请求**（LiteLLM、new-api）：最小的真实请求。对话模型是一条用户消息加 `max_tokens: 1`，向量模型是一段短输入；
  超时 `health_check_timeout_seconds`（默认 60）。OpenAI 的推理模型不接受 `max_tokens`，一个 token 也答不出来，所以对话探测被
  400/422 拒绝时改用 `max_completion_tokens: 16` 再发一次，16 是 new-api 渠道测试给推理模型用的值。
  仍然返回 400/422 记为健康：上游在线、凭据有效，只是探测请求不适合这个模型（路由同样不把这两个状态码算作部署的故障）。
- **失败分类**（Bifrost 的 `failureclass.go`），存进 `failure_class`：

  | `failure_class` | 判定 | 状态 |
  |---|---|---|
  | `rate_limit` | 429 | `rate_limited` |
  | `quota` | 402，或响应里有余额/额度用完的字样 | `unhealthy` |
  | `credential` | 401、403 | `unhealthy` |
  | `model_gone` | 404 | `unhealthy` |
  | `transient` | 其余 4xx、5xx | `unhealthy` |
  | `timeout` | 408，或没有在限定时间内响应 | `unhealthy` |
  | `unreachable` | 连不上 | `unhealthy` |

  额度字样用的是 new-api 默认的 `AutomaticDisableKeywords`（另加 OpenAI 的错误码 `insufficient_quota`），先于状态码判断，
  因为厂商把余额用完报成 429 或 403 的情况和 402 一样常见。
- **被限流不算故障**（new-api 默认只因 401 和额度字样禁用渠道；Portkey 熔断默认只计大于 500 的状态码；Envoy 离群检测只计 5xx）。
  限流是账号的状态，会自己恢复，所以单独记为 `rate_limited`，在页面上用警告色显示，**不改变它在路由里的位置**；
  真实调用遇到 429 时由上面的冷却处理。只有 `unhealthy` 的部署被路由排到最后，直到之后某一轮发现它恢复；
  一个名字的部署全都不健康时照常尝试。
- **尽量少占上游的额度**：
  - 上一个间隔内有真实调用成功的模型不探测，直接记为健康，`source` 为 `traffic`（Bifrost 只靠真实流量判断健康，完全不主动探测）。
    这个判断用的是本进程里的调用，多个数据面进程时，没接到这个模型流量的进程仍会探测。
  - 被限流的模型至少跳过下一轮；上游给了 `Retry-After` 就按它等，最长一小时。跳过期间保留上一次的结果。
- 结果写入 `t_ai_gateway_model_health`，每个模型一行，每轮覆盖，不留历史；管理面的模型目录带出 `health`
  （状态、失败分类、来源、状态码、错误信息、耗时、检查时间）。检查关闭时，或结果比三轮更旧时，`health` 为空。
- 探测是真实的、会计费的上游调用，但不写调用记录，不计入任何限额。每个数据面进程各跑各的检查。
- 没有做：按探测结果自动关闭模型（new-api 的自动禁用）。现在只调整路由顺序和显示，开关仍由管理者决定。

### 请求流程

1. 认证 Key，得到 `key_id`、`tenant_id` 和模型范围（第 5 节）。
2. 只解析请求体里的 `model`、`stream`、`user`，其余字段交给上游校验。请求体有大小上限。
3. 在租户目录里按名字找到它的部署，依次去掉已关闭的（一个不剩返回 404）、不在 Key 范围内的（403）、
   能力与端点不符的（400），再用 `resolve_config()` 取每个部署的上游地址、凭据和模型名，都不能服务时返回 503。
   Key 的范围对每个部署都生效，所以换部署不会把调用带到 Key 没被授权的连接上。
4. 按上面的多部署规则选一个部署，改写请求并转发；失败且还没有向调用方发出任何内容时换下一个。认证和解析用的数据库会话在此之前关闭，流式期间不持有会话或事务。
5. 把上游响应原样返回，同时读取用量。
6. 请求结束后写一行调用记录，包括失败和客户端中途断开的情况。

约定：

- **流式用量**：强制让上游返回用量尾块。客户端自己没有要求时，仅含用量的尾块由网关消费，不转发给客户端。
- **响应里的 `model`**：首期原样返回上游值（见第 11 节的未决事项）。
- **只有一个部署的名字**：一次请求对应一次上游调用，不重试。
- **客户端断开**：关闭上游连接，调用记录的状态码记 499、`error_type` 为 `client_closed`；断开前没有收到用量时，用量为 `unavailable`。
- **流的完成**：结束标记 `data: [DONE]` 送达即视为完成，网关不再读上游。SDK 读到结束标记就会立刻关连接，
  而有些上游会保持连接，所以在这之后的断开不算"客户端断开"。
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
| 模型的能力与端点不符 | 400 | `model_endpoint_mismatch` |
| Key 无权访问该模型 | 403 | `model_not_allowed` |
| 模型名不存在，或模型已关闭 | 404 | `model_not_found` |
| 请求体超过上限 | 413 | `request_too_large` |
| 触发限流、并发上限或配额 | 429 | `rate_limit_exceeded`；`concurrency_limit_exceeded`；`insufficient_quota`（第 7 节） |
| 上游返回 401 / 403 | 502 | `upstream_auth_error`，避免调用方误以为是自己的 Key 有问题 |
| 上游返回其他 4xx | 原样返回 | 上游的错误体，保留 `Retry-After` |
| 上游 5xx；连接失败 | 502 | `upstream_error`；`upstream_unreachable` |
| 开放的模型当前不能服务（在模型管理里停用、厂商不在收录范围） | 503 | `model_unavailable` |
| 上游超时 | 504 | `upstream_timeout` |

## 5. 认证与授权

- 调用方用 `Authorization: Bearer <key>`。服务端按 `key_id` 查行，用常量时间比较校验
  `secret_hash`，再检查状态和有效期。首期每个请求直接读库，撤销和范围收窄对新请求立即生效；
  进行中的流不被中断。
- 已迁入的 Key 通过唯一的摘要索引定位同一张 Key 表，再走上述校验；摘要索引单独命中不会授权。
- `GET /v1/models` 和实际调用使用同一个授权判断函数。
- 调用方只能提交目录里的模型名（第 3 节）。请求里不能指定上游地址、上游凭据或内部模型 ID。
- 三类凭据互不通用：网关 Key 只能调用数据面；既有 RAG SDK 令牌和登录态不能调用数据面；
  网关 Key 不能调用管理接口和既有 RAG 接口。
- 管理接口要求登录态。来源为 `AuthenticationSource.SDK_API_TOKEN` 的身份
  （`api/identity/principal.py`）被拒绝，并要求与模型管理（`provider_api.py`）相同的租户权限。
- `created_by` 只记录管理操作人。调用按 `key_id` 归属，不用创建者的权限替代。

## 6. 调用记录与用量

模型目录、Key 允许列表和调用记录使用同一个注册模型 ID。
旧库通过 [模型身份迁移](../../api/db/services/MODEL_PROVIDERS.md#existing-installations)
统一转换这三处引用，保留原有 Key 范围、别名、注册顺序和历史用量。
OpenAI 兼容端点继续接收目录中的调用名称。

- 每个请求在结束后写一行。写入失败不影响已经返回的响应，但要记错误日志和指标。
- 用量优先取上游返回值。取不到时标 `unavailable`，数值留空；失败或被取消的请求同样可能消耗了资源。
- 网关不更新 `TenantModel.used_tokens`。该累计值仍只由 `LLMBundle` 写入，避免两处计数。
- 查询按 Key、模型、时间范围、状态和 `end_user` 筛选。`/usage` 仍直接对调用记录分组；
  概览的 7 / 30 天窗口、Key 的配额已用量读按日汇总表，24 小时窗口按小时分桶，仍读调用记录。
- **保留与清理**（`api/model_gateway/retention.py`）：`log_retention_days` 大于 0 时，数据面进程每隔
  `log_cleanup_interval_seconds`（默认一天）删除早于保留期的调用记录，每批 1000 行，单次最多运行 300 秒，
  连续 3 批失败就结束本次、等下一次；做法取自 LiteLLM 的 `SpendLogCleanup`。多个数据面进程同时清理互不影响。
  保留期最少 62 天：概览的错误码分布、首字时延和 24 小时窗口仍读调用记录。停用、撤销 Key 不删除它的历史记录。

### 费用

- **价格来源**（`api/model_gateway/pricing.py`）：直接读随 `litellm` 依赖安装的价格表
  `model_prices_and_context_window_backup.json`（LiteLLM 代理拉不到远端时用的同一份），不导入 `litellm` 本身，
  也不维护自己的价格数据。按「厂商 → LiteLLM `litellm_provider`」的对照表加上游模型名查找；自部署和转售类
  厂商不在对照表里，没有价格，除非管理者在模型上填了价格。升级 `litellm` 依赖即更新价格。
- **算法**与 LiteLLM 的 `generic_cost_per_token` 对文本 token 的处理一致：缓存命中的输入 token 从输入里扣出、
  按 `cache_read_input_token_cost` 计（没有该价格时按输入价）；输入超过 `_above_<n>k_tokens` 阈值或落在
  `tiered_pricing` 某一档时，整次调用按那一档的价格计。
- **记账时机**：调用结束写调用记录时，按当时的价格算出 `cost` 存下来，之后改价不改写历史。
  模型没有价格或用量未知时 `cost` 为空，汇总里不计入。迁移之前的调用记录没有费用。
- 单位是美元 / token；管理页面按「美元 / 每百万 token」显示和输入。
- **定时拉取价格表**（`api/model_gateway/price_refresh.py`）：`price_map_refresh_hours` 大于 0 时（默认 24，设为 0 关闭），每隔这么久从
  `price_map_url` 拉一次 LiteLLM 的价格表（留空即 LiteLLM 仓库里的 `model_prices_and_context_window.json`，
  也可以是内部镜像地址或本机文件路径）。这是 LiteLLM 自己的做法：运行时拉远端，包里那份是兜底。这样新模型的价格
  不必等 `litellm` 依赖升级，依赖版本可以一直跟着上游走。拉到的内容按 LiteLLM 的校验规则检查（至少 50 个模型、
  不少于当前在用条目数的一半），通过才替换；拉取失败或校验不过时继续用上一次成功的结果，从没成功过就用包里那份。
  数据面有定时循环；主 API 没有，改为在打开模型目录时按需在后台触发。两个进程各拉各的，可能短暂不一致。
  它是一个对外网络请求，访问不了时只会记一条警告、继续用现有价格。已经存进调用记录的费用不受影响。
- 尚未做：图像、音频、按次计费等非 token 价格；按服务层级（batch / flex）区分价格。

## 7. 限额

限额都是可选的，留空表示不限。没有配置任何限额的请求不经过限额器。

| 维度 | 字段 | 说明 |
|---|---|---|
| 单个 Key | `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 每分钟请求数、每分钟 token 数、并发上限 |
| 单个 Key | `token_quota`、`quota_period` | token 配额，按 UTC 自然日（`day`）或自然月（`month`）重置；两个字段必须一起设置 |
| 单个模型 | `rpm_limit`、`tpm_limit`、`max_parallel_requests` | 存在模型设置行上，该模型所有 Key 合计，保护上游容量；多建 Key 不能绕过，换用别名或带前缀的名字也不能绕过 |

实现在 `api/model_gateway/limits.py`：

- **准入**：请求解析出模型后、转发前检查。先看配额，再看 token 速率、请求速率和并发。
  任何一项拒绝都不消耗其他项的额度。
- **token 事后结算**：用量在调用结束时才知道，所以越线的那次调用会完成，下一次被拒绝。
  不做预留和对账。用量未知的调用不计入 token 速率和配额。
- **速率窗口**：固定的一分钟窗口。
- **配额**：每个周期第一次用到时从按日汇总里读出已用量作为起点，之后在计数器上累加，所以重启后配额依然有效。
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

- **计数放在哪里由 `state_store` 决定**，默认 `redis`。`memory`：在数据面进程内，多个进程各自计数，重启后每分钟窗口清空。
  `redis`（`api/model_gateway/redis_state.py` 的 `RedisLimiter`）：四种限额都放在 Redis，多个进程合并计数，重启不清空。
  做法照 LiteLLM 的 v3 限流器（`parallel_request_limiter_v3.py`）：一个 Lua 脚本先检查所有维度、全部通过才计数，
  所以无论多少进程并发，被拒绝的请求都不消耗任何额度；窗口仍是固定的一分钟。并发槽位带过期时间，进程崩溃留下的槽位
  在 `total_timeout_seconds` 加一分钟后自动释放。**Redis 不可用时默认放行并退回本进程计数**（LiteLLM 也是这样），
  恢复后自动切回；期间的计数不会补记到 Redis。
- 推理服务本身的并发上限仍由推理服务保证；网关限制的是准入。
- 严格预算（不可超支）需要预留、幂等结算和崩溃恢复，属于单独的扩展。

## 8. 管理面

管理接口挂在主 API 下，文件为 `api/apps/restful_apis/model_gateway_api.py`，由现有的路由自动注册加载，
响应沿用主 API 的信封。handler 用 `async def` 加 `Depends(get_async_db)`。

| 路由 | 用途 |
|---|---|
| `GET /api/v1/model-gateway/catalog` | 全部已登记模型的目录（第 3 节的属性、近 7 天请求数、生效价格 `pricing` 及其来源、管理者定的 `custom_pricing`、价格表里的上下文长度），以及全部连接和租户默认值 |
| `PATCH /api/v1/model-gateway/catalog/models/{model_id}` | 开放或关闭模型，设置别名、首选、多部署的权重和顺序（`weight`、`order`）、说明、模型级限额和价格（三个价格字段，传 `null` 回到价格表；只填输出价或缓存价而没有输入价返回 422）；别名已被另一个模型使用时返回 409 |
| `PATCH /api/v1/model-gateway/catalog/instances/{instance_id}` | 开放或关闭一个连接下的全部模型 |
| `PATCH /api/v1/model-gateway/settings` | 设置新登记模型的默认开放状态 |
| `GET` / `POST /api/v1/model-gateway/keys`，`PATCH .../keys/{id}`，`POST .../keys/{id}/revoke` | 创建、修改元数据和可调用范围、启停、撤销 Key |
| `POST /api/v1/model-gateway/keys/import/preview`，`POST .../keys/import/apply` | 平台管理员预览或原子导入当前租户的摘要清单，见[迁移已有调用方 Key](#迁移已有调用方-key) |
| `POST /api/v1/model-gateway/keys/{key_id}/rotate` | 轮换：换新密钥并只在本次响应返回；请求体可带 `grace_period_seconds`（0 到 30 天，默认 0），旧密钥在这段时间内仍可用 |
| `GET /api/v1/model-gateway/overview` | 接入地址、数据面健康状态、计数，以及最近 1 / 7 / 30 天的请求、失败、token、首字时延中位数、时间序列（每个时间桶的请求数、失败数、`total_tokens`、`prompt_tokens`、`completion_tokens` 和 `spend`；`models` 按窗口内最忙的 5 个模型拆分请求数、token 和费用，其余合并在 `other` 下，键的顺序见 `series_models`；`latency` 是该桶内成功调用的耗时 P50、P95 和首字中位数，没有成功调用时为 `null`），`totals` 另有成功调用的 `duration_p50_ms` / `duration_p95_ms`（耗时分位只能从调用记录算，按日汇总表里没有，所以回看范围受调用记录保留期限制；失败的调用不计入，拒绝在几毫秒内返回会把慢的上游算快）、Top 模型与 Key（含 `spend`）、最近失败；`totals.spend` 是窗口内的美元费用 |
| `GET /api/v1/model-gateway/logs` | 查询调用记录；`status_code` 按确切的状态码筛选，可与 `outcome` 同用 |
| `GET /api/v1/model-gateway/usage` | 按 Key、模型、日期或 `end_user` 汇总用量 |

Key 的范围在创建和修改时校验：模型 ID 必须是本租户登记的模型，连接 ID 必须是本租户的连接。
限额字段在创建和修改时都可以带；修改时显式传 `null` 表示取消该项限额。Key 列表里的 `quota_used`
是当前周期的已用 token（没有配额的 Key 为 `null`）。
调用记录按页查询（每页最多 100 条），用量汇总默认取最近 7 天，日期按 UTC 划分。
汇总里的 token 只累计用量已知的调用，`usage_unknown` 给出未计入的调用数。概览的时间桶也按 UTC 划分，
空桶补零；`base_url` 在监听通配地址且未配置 `public_base_url` 时为空，页面按当前主机名补全。
Web 管理页面在独立 Web 仓库的 `src/pages/settings/model-gateway/`，入口是设置里的「模型网关」：
概览、模型、API Key（明文只在创建后的弹窗里显示一次）、调用记录四个分区。
网关未启用时，页面显示「未启用」而不是错误。

## 9. 与 RAGFlow 跟进的隔离

网关代码放在 `api/model_gateway/`：`catalog`（目录桥）、`directory`（开放状态与命名）、`keys`、`store`（数据面持久化）、
`relay`、`usage`、`limits`、`pricing`（价格与费用）、`retention`（调用记录清理）、`app` 和 `server`（数据面进程）、
`management` 和 `schemas`（管理操作）。
它对现有文件的改动限于下表，全部是追加：

| 文件 | 改动 |
|---|---|
| `api/db/db_models.py` | 末尾追加网关自有表。建表函数只识别这个文件命名空间里的模型类，所以表定义必须在这里 |
| `configs/alembic/versions/` | `2c4e6a8b0d1f` 加健康检查表和 Key 轮换的三列；`3d5f7b9c1e2a` 给健康结果加失败分类和来源，状态多一个 `rate_limited`；`d1f3a5c7e9b2` 建表；`f3b5d7e9a1c2` 以模型目录取代发布（见下）；`0a2c4e6f8b1d` 加费用、模型价格、按日汇总表和清理用的索引，并回填汇总；`1b3d5f7a9c2e` 加权重、顺序和尝试记录，别名索引改为不唯一，并把已标记首选的模型的顺序设为 1（它原来独占共享的名字，升级后仍然排第一，其余同名模型作为后备） |
| `api/db/services/model_provider_service.py` | 新增只读的 `instances_for_owner()`，供目录列出没有模型的连接 |
| `common/app_config.py` | 新增 `model_gateway` 配置段 |
| `api/db/services/model_deletion_policy.py` | 目录取代发布后，网关不再登记删除保护引用 |
| `api/apps/restful_apis/model_gateway_api.py` | 新文件，管理路由 |
| `pyproject.toml` | 新增 import-linter 契约；把 `api/model_gateway` 纳入 mypy 严格检查范围 |
| `tests/support/integration_suites.py` | 登记集成测试文件 |
| [模型配置生命周期](../model-provider-lifecycle.md) | 引用表说明网关不阻止模型删除 |

不修改：`core/llm/*`、`api/db/services/llm_service.py`、`api/db/services/tenant_llm_service.py`、
`api/apps/restful_apis/openai_api.py`、`api/utils/api_utils.py`、`api/apps/__init__.py`，以及 `APIToken`。

依赖规则：

- `api.model_gateway` 不得导入 `core.llm`、`agent`、`api.apps`、`api.db.services.llm_service`、
  `api.db.joint_services`。先按直接导入约束，与仓库里其他契约的做法一致。
- 读取模型配置只通过一个模块（目录桥），它调用 `model_provider_service` 的
  `resolve_config()`、`models_for_owner()` 和 `instances_for_owner()`。网关其余部分不接触模型相关的 ORM。

模型删除不受网关阻止：目录随登记信息变化，被删除的模型从目录里消失；它的设置行留下但不再生效，
Key 范围里的对应 ID 保留，管理页面显示为已失效。

**从发布迁移到目录**（`f3b5d7e9a1c2`）：每个模型取一条发布记录（优先启用的，再按创建时间），公开名与模型名
不同的转为别名，启用状态、说明和限额照搬；对已经用过网关（有发布记录或 Key）的租户，从未发布的模型写成
关闭，保持迁移前的暴露范围；Key 的公开模型 ID 换成已登记模型 ID，找不到的丢弃；最后删除发布表。
同一模型的其余发布名不再可用。降级只在网关没有数据时可做。
`d1f3a5c7e9b2` 校验按模型先建的表时允许多出后续迁移增加的列。

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
| 模型目录（**已实现**） | 去掉发布；目录、开放开关、命名规则、别名与首选；Key 按连接或模型授权；概览；迁移 | 登记即可授权；同名模型名字稳定；迁移保持原暴露范围；按连接授权包含以后开放的模型 |
| 清理、费用与按日汇总（**已实现**） | 调用记录保留期清理；按 LiteLLM 价格表和管理者定价记费用；按日汇总表及回填 | 清理分批且不动汇总；两条引导路径的 schema 一致；费用、汇总与调用记录逐项对得上 |
| 多部署（**已实现**） | 同名或同别名的模型共同承接一个名字；顺序、权重、冷却、换部署重试；调用记录里的尝试记录 | 首选部署失败时由后备部署应答且记在后备名下；Key 的范围对每个部署生效；迁移后原首选模型仍排第一 |
| 共享状态、健康检查、Key 轮换、`/v1/messages`、价格表拉取（**已实现**） | Redis 限额计数与冷却共享；后台健康检查；带宽限期的 Key 轮换；Anthropic 协议入口；定时拉取 LiteLLM 价格表 | 两个限流器共用一个 Redis 时合并计数，Redis 不可用时退回本进程；健康检查结果进目录且不写调用记录；宽限期内新旧密钥都可用；Anthropic SDK 完成普通和流式调用；拉取失败或内容异常时价格不变 |
| P2 余项（未实现） | rerank | 单独验收 |
| P3 按需 | 非 OpenAI 协议厂商；`/v1/responses`；按花费的预算与告警；正文日志（手动开启）；管理审计；严格预算 | 每项单独立项和验收 |

验证按 [AGENTS.md 的验证表](../../AGENTS.md#验证) 执行：

- **单元**：Key 的生成、解析和校验；授权判断；命名与开放规则；流式用量解析（客户端是否要求用量两种情况）；
  错误映射；概览的地址推导和数据面探测。上游用受控的假服务，不依赖真实模型。
- **集成**：scratch 库上两条引导路径的建表和迁移；发布数据迁移到目录；目录、Key 范围和调用记录独立读回；网关不阻止模型删除；概览汇总。
- **契约**：同一条模型配置，网关与 `core/llm` 驱动算出的上游地址和模型名一致。
- **消费者**：集成测试用真实的 OpenAI Python SDK 调用网关。该 SDK 的 embeddings 默认请求 base64 编码，
  已覆盖。
- **真实上游**：`tests/manual/model_gateway_live.py` 在 scratch 库里登记环境变量给出的模型，经网关做一次
  普通调用和一次流式调用，并读回调用记录。它不随测试套件运行；每接入一类新的上游，手动跑一次。
  通义千问已跑过（2026-11-17，qwen-plus 与 text-embedding-v4，默认地址）：普通、流式和向量调用都返回 200，
  用量都来自上游，调用方没有收到多余的用量尾块；DashScope 兼容接口直接支持 base64 编码的向量。

对应的测试文件：`tests/unit/test_model_gateway_*.py`（Key、用量、目录契约、命名与开放、数据面、管理路由、概览）和
`tests/integration/test_model_gateway.py`（迁移两条引导路径、发布数据迁移、SQL 持久化、Key 生命周期、用量汇总与概览）。

通用性验收单列：说明和标签为空时照常调用和统计；改名不改写历史；标签不能改变授权和路由；
撤销 Key 后历史仍可查；全程不需要创建组织、应用或项目记录，也不需要发布模型。

## 11. 未决事项

**待做：Anthropic 协议上游的原生透传（2026-11-26 记录）。** 现在转发规则只有 OpenAI 协议，上游是 Anthropic 本家或兼容
Anthropic 协议的厂商时，`/v1/messages` 的请求会先转成 chat completion 再由上游转回去，`cache_control`、带签名的思考块、
托管工具都过不去。参考实现都是"上游说 Anthropic 协议就原样透传"：new-api `relay/channel/claude/adaptor.go`
（`ConvertClaudeRequest` 原样返回，设 `x-api-key`，`anthropic-version` 缺省 `2023-06-01`，合并转发 `anthropic-beta`）、
Bifrost `integrations/anthropic.go` 的 `checkAnthropicPassthrough`、LiteLLM 主干 `messages/native/handler.py`。
要补的是：转发规则加协议标记和 `messages` 端点；转发 `anthropic-version` / `anthropic-beta`；按 `message_start` / `message_delta`
读用量的解析器；价格里加缓存写入价。另外 LiteLLM 锁在 `~=1.82.0`，升级后请求侧的清理和流式转换可以换回它的实现。

**待做：预算告警（2026-11-25 记录，有需求时再做）。** 现在只有 Token 配额：用完即拒绝，管理页面上配额条超过 80% 变成警告色，
没有任何主动通知，也没有按花费（美元）设的预算。要补的内容和可以照搬的设计：

- 按花费的预算：Key 上加 `max_budget` 和周期，沿用 LiteLLM Key 的 `max_budget` / `budget_duration`；花费已经随调用记录和按日汇总存下来，
  计数方式可以照 Token 配额在 `RedisLimiter` 里再加一个计数器。
- 告警阈值：LiteLLM 的 `soft_budget`（越线只告警、不拒绝），以及在预算的固定比例（如 80%）时提醒。
- 通知渠道还没定（Webhook、邮件或站内通知），这是开工前唯一需要先决定的事；LiteLLM 用的是 Slack / 通用 Webhook。
- 需要一次迁移（Key 的预算列，以及"本周期已告警过"的标记，避免重复通知）。

1. 响应里的 `model` 是否改写为调用名。改写流式响应需要逐块解析和重新序列化，与原样转发冲突。
2. （已定）多个数据面进程通过 Redis 共享限额计数；Redis 不可用时放行并退回本进程计数，见第 7 节。
3. 调用记录写入失败时，是否需要「拒绝新请求」的严格模式。
4. 管理接口是否需要自动化凭据（独立于登录态的管理 Key）。
5. embeddings 的 base64 编码依赖上游支持。上游不支持时，由网关转换还是原样返回上游错误。
6. 数据面是否提供挂载到主 API 的部署方式，作为独立进程之外的选项。

## 12. 参考与取舍

以下是 2026-11-15 与 2026-11-17 核对的同类实现，链接指向各项目默认分支，括号内是核对时的提交。
它们用于说明取舍，不表示这些项目策略相同，也不表示本设计复刻其存储结构。

| 取舍 | 参考 |
|---|---|
| 承接调用方已有 Key，复用 Key 管理和轮换。管理员通过摘要清单导入 | [LiteLLM v1.111.0 自定义凭据校验](https://github.com/BerriAI/litellm/blob/e3370c9d6152cc4407a55e06c101e3c7dd4168ab/litellm/proxy/management_endpoints/key_management_endpoints.py#L1842-L1859)、[Unkey SDK 的 MigrateKeys](https://pkg.go.dev/github.com/unkeyed/sdks/api/go/v3#Keys.MigrateKeys)（2026-11-27 核对） |
| 迁移 CLI 逐条报告未映射约束。MultiRAG 遇到未解决项时阻止整批写入 | [Bifrost 迁移 CLI 的归属、预算与未映射字段](https://github.com/maximhq/bifrost/blob/172617bb045c6f43d697843c115e64f0155ed143/scripts/bifrost-migration-cli/virtualkey.go#L18-L107)（2026-11-27 核对） |
| 限额计数放 Redis，一个 Lua 脚本先查后记，固定一分钟窗口，Redis 不可用时退回进程内 | [LiteLLM `parallel_request_limiter_v3.py`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/hooks/parallel_request_limiter_v3.py)（随依赖安装的版本，2026-11-25 核对） |
| 探测是 `max_tokens: 1` 的真实请求，结果落表 | [LiteLLM `health_check.py`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/health_check.py)、`LiteLLM_HealthCheckTable`（同上） |
| 推理模型的探测用 `max_completion_tokens: 16`；被限流不算故障，只有 401 和额度字样才算账号问题 | [new-api `channel-test.go`](https://github.com/QuantumNous/new-api/blob/main/controller/channel-test.go)、[`operation_setting.go`](https://github.com/QuantumNous/new-api/blob/main/setting/operation_setting/operation_setting.go)（2026-11-25 核对） |
| 失败按限流、凭据、额度、模型不存在、瞬时错误、超时分类；健康可以只从真实流量得出 | [Bifrost `failureclass.go`](https://github.com/maximhq/bifrost/blob/main/core/failureclass.go)（2026-11-25 核对） |
| 429 默认不计入熔断或离群 | [Portkey Circuit Breaker](https://portkey.ai/docs/product/ai-gateway/circuit-breaker)、[Envoy Outlier Detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier) |
| 轮换保留 Key 本身，旧密钥有宽限期 | LiteLLM `/key/{key}/regenerate` 的 `grace_period`（同上） |
| Anthropic 协议入口的请求和整段响应用现成的转换器；流式事件、ping、模型列表形状照 LiteLLM 主干、new-api、Bifrost 和 [Claude Code 网关要求](https://code.claude.com/docs/en/llm-gateway-protocol) | [LiteLLM Anthropic adapter](https://github.com/BerriAI/litellm/tree/main/litellm/llms/anthropic/experimental_pass_through/adapters)（同上）；[new-api `relay-router.go`](https://github.com/QuantumNous/new-api/blob/main/router/relay-router.go) 同样在 OpenAI 协议渠道前提供 `/v1/messages` |
| 运行时拉取价格表，包里那份做兜底，校验后才替换 | [LiteLLM `get_model_cost_map.py`](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/get_model_cost_map.py)（同上） |
| 同一个名字下的多个模型是它的部署；`order` 先低后高、`weight` 加权随机；失败后冷却 5 秒，条件按状态码和一分钟失败率 | [LiteLLM `cooldown_handlers.py`](https://github.com/BerriAI/litellm/blob/main/litellm/router_utils/cooldown_handlers.py)、[`simple_shuffle.py`](https://github.com/BerriAI/litellm/blob/main/litellm/router_strategy/simple_shuffle.py)（随依赖安装的版本，2026-11-25 核对）；[new-api 渠道优先级与权重](https://github.com/QuantumNous/new-api) |
| 调用记录里保留每一次尝试 | [Bifrost 日志的 `attempt_trail`](https://github.com/maximhq/bifrost) |
| Key 除名称外的字段全部可选；归属层级可空 | [LiteLLM `schema.prisma`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/schema.prisma)（`09ce4f50`）、[GPUStack `api_keys.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/api_keys.py)（`aba5c665`） |
| Key 为「公开标识 + 秘密」两段，只存摘要，明文只返回一次 | 同上 GPUStack；[TensorZero 认证文档](https://github.com/tensorzero/tensorzero/blob/main/docs/operations/set-up-auth-for-tensorzero.mdx) |
| 同协议上游走 HTTP 转发，强制上游返回流式用量 | [new-api `relay-router.go`](https://github.com/QuantumNous/new-api/blob/main/router/relay-router.go)（`b388140a`）、[GPUStack `routes/openai.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/routes/openai.py) |
| 调用记录在请求结束后写一行，默认不存正文 | [LiteLLM `schema.prisma`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/schema.prisma) 的 SpendLogs、[new-api `log.go`](https://github.com/QuantumNous/new-api/blob/main/model/log.go)、[GPUStack `model_usage_details.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/model_usage_details.py) |
| token 限额事后扣减 | [Higress `ai-token-ratelimit`](https://github.com/higress-group/higress/blob/main/plugins/wasm-go/extensions/ai-token-ratelimit/README_EN.md)（`0316bc90`）、[Bifrost `budget.go`](https://github.com/maximhq/bifrost/blob/dev/framework/configstore/tables/budget.go)（`a3cd2b5b`） |
| 不设发布步骤，已配置的模型即可授权；按提供商整组授权包含以后的模型 | [new-api `model/ability.go`、`controller/model.go`](https://github.com/QuantumNous/new-api)（`669d7749`）、[LiteLLM `auth_checks.py`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/auth/auth_checks.py)（`0cc072c9`）、[Bifrost `account.go`](https://github.com/maximhq/bifrost/blob/dev/core/schemas/account.go)（`09edfa35`） |
| 规范名带提供商前缀，避免同名冲突 | [Portkey Model Catalog](https://portkey.ai/docs/product/model-catalog)、[GPUStack `model_routes.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/model_routes.py)（`8e252645`） |
| 上层开关是上限，Key 在上限内收窄；建 Key 时选「全部 / 自定义」 | [百炼 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)、[方舟 API Key](https://docs.volcengine.com/docs/82379/1361424) |
| 默认资源范围内即可签发 Key；部门和成本中心用标签承载 | [OpenAI 项目 API Key](https://developers.openai.com/api/reference/resources/organization/subresources/projects/subresources/api_keys)、[百炼业务空间](https://help.aliyun.com/zh/model-studio/use-workspace)、[方舟 API Key](https://docs.volcengine.com/docs/82379/1541594) |
| 费用用 LiteLLM 的价格表、字段名和算法，不自建价格数据 | [LiteLLM `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)、[`llm_cost_calc/utils.py`](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/llm_cost_calc/utils.py)（`85ba26ff`） |
| 按日汇总表的列名和 upsert 累加；调用记录分批清理 | [LiteLLM `schema.prisma` 的 DailyUserSpend、`daily_spend_bulk_upsert.py`、`spend_log_cleanup.py`](https://github.com/BerriAI/litellm/tree/main/litellm/proxy/db)（`85ba26ff`） |
| 限流响应头和 `x-request-id` | [OpenAI 限流说明](https://developers.openai.com/api/docs/guides/rate-limits) |

更细的第三方源码对照不在仓库文档里维护，分层规则见
[CHN-ADR-05](../channel-program/DECISIONS.md#chn-adr-05--文档分层入库讲我们的代码本地讲别人的代码)。
