# Channel 前后端契约

> **契约版本**：`channel-api/v1` · **最后变更**：2026-08-05 · **变更提交**：待回填
>
> 本文件是 channel 前后端接口的**唯一真源**。前端仓不得保存第二份契约描述。
> 契约变更 = 改本文件 + bump 版本 + 更新 `web:src/api/channel.ts` 的 `CHANNEL_API_VERSION`
> （见 [PROGRESS 维护协议第 4 条](PROGRESS.md#维护协议mandatory--任何处理本文档条目的-agent-必须遵守)）。
>
> **v1 记录的是「今天实际是什么」，不是「应该是什么」。** 已知不符之处逐条标了 ⚠️
> 并挂到对应的 CHN ID 上——修好之后回来把标记去掉。

## 0. 这份文件是怎么来的

它是从 `web:src/api/__tests__/channel.test.ts` 的 **11 条断言**反推出来的（CHN-X1）。
那个文件今天是**事实上的契约测试却没有任何文档在它背后**——它在 CI 里跑（`npm run test:api`
的 glob 命中 `src/api/__tests__/*.ts`），所以它断言什么，什么就是实际生效的契约。
本文件让那些断言有一份可以对照的文档。

**其中有三条断言编码的是「今天的错误行为」而非「意图行为」**，见 §6。

---

## 1. 端点清单

前端统一走 `apiClient`，`baseURL` 为 `${API_BASE_URL}/api`，路径带 `/v1`。
断言见 `channel.test.ts:255-278`（`'channel API uses RESTful /api/v1 endpoints'`）。

| 方法 | 路径 | 用途 | 前端消费方 | 版本 |
|---|---|---|---|---|
| GET | `/chat-channels/providers` | provider manifest 列表 | `channelAPI.listProviders` | v1 |
| GET | `/chat-channels` | 租户渠道列表（含 runtime） | `channelAPI.list` | v1 |
| GET | `/chat-channels/{id}` | 单渠道详情 | `channelAPI.get` | v1 |
| POST | `/chat-channels` | 创建渠道 + binding | `channelAPI.create` | v1 |
| PATCH | `/chat-channels/{id}` | 更新连接配置与 binding | `channelAPI.update` | v1 |
| DELETE | `/chat-channels/{id}` | 删除渠道（级联四张表） | `channelAPI.remove` | v1 |
| PUT | `/chat-channels/{id}/binding` | 只改绑定目标 | `channelAPI.putBinding` | v1 ⚠️ |
| POST | `/chat-channels/{id}/enable` | 启用 binding | `channelAPI.enable` | v1 |
| POST | `/chat-channels/{id}/disable` | 停用 binding | `channelAPI.disable` | v1 |
| GET | `/chat-channels/{id}/runtime` | 脱敏后的运行状态 | `channelAPI.runtime` | v1 |

⚠️ **`PUT /{id}/binding` 在前端是死代码**：`channelAPI.putBinding` 有定义、无生产调用点，
而且 `channel.test.ts:219,252` 两处**主动断言 `putCalled === false`**。所有绑定修改都被塞进
PATCH，导致改一次绑定必须连带重发整个 `config`。→ 见 §6-C。

---

## 2. 写请求形状

### 2.1 创建（`POST /chat-channels`）

断言见 `channel.test.ts:53-66`、`:191-220`。

```jsonc
{
  "name": "Leadership demo",          // 已 trim
  "channel": "feishu",                // 仅 create 带；update 不带
  "config": {
    "credential": { "app_id": "cli_xxx", "app_secret": "new-secret" },
    "domain": "feishu",
    "allowed_open_ids": ["ou_1", "ou_2"]
  },
  "binding": {
    "target_type": "multirag.canvas_agent",
    "target_id": "agent_1",
    "target_revision_id": "revision_1",  // canvas 必填；dialog 必须为 null
    "policy": { "private_chat_only": true },
    "enabled": false                     // 创建时恒为 false
  },
  "status": 0                            // 创建时恒为 0
}
```

**创建后 binding 恒为停用**：`createRequest?.status === 0` 且 `binding.enabled === false`
（`:216-217`）。启用是独立的一次 `POST /{id}/enable`。

### 2.2 更新（`PATCH /chat-channels/{id}`）

同形，但**不带 `channel`**（`:40` 断言 `'channel' in payload.connection === false`），
且 `binding.enabled` 透传当前值。

### 2.3 凭据写入语义（**铁律，不可回退**）

| 规则 | 断言 |
|---|---|
| secret 字段的表单默认值恒为空串 | `:123` `defaults.secrets.app_secret === ''` |
| **空 secret 必须整个从 payload 里剔除**，不能发空值——空 = 保持服务端现有密钥 | `:44-46` 三条 |
| 凭据只出现在 `config.credential` 下，**绝不出现顶层 `secret`/`app_secret`** | `:44-45`、`:65` |
| 服务端读响应绝不回显密文或明文，只给 `{configured, version}` | `:105-124` 用一个含 `must-not-reach-form` 的伪响应固化了这条 |

`allowed_open_ids` 的解析规则：按 `/[\n,]/` 切分 → trim → 去空 → **去重**。
断言 `:26`+`:47`：`'ou_1\nou_2, ou_1'` → `['ou_1', 'ou_2']`。

---

## 3. 运行时状态词表（服务端唯一真源）

**前端不得扩充本表。** 服务端定义在 `api/channel_runtime/schemas.py:44`：

```python
RuntimeState = Literal["waiting", "starting", "connected", "stopping", "stopped", "error"]
```

| 值 | 含义 | 备注 |
|---|---|---|
| `waiting` | 期望启用但 worker 还没起来 | ⚠️ **与「supervisor 根本没在跑」不可区分**——见 §7 |
| `starting` | worker 正在建立连接 | |
| `connected` | 连接已建立 | 唯一的「健康」状态 |
| `stopping` | 正在优雅停止 | |
| `stopped` | 已停止 | |
| `error` | 运行错误，看 `last_error_code` | |

⚠️ **前端历史上自建了一份 12 条的词表**，其中 `pending` / `running` / `healthy` / `online` /
`disabled` / `failed` 六个服务端**永远不会返回**；`utils.ts:10-13` 的 `isRuntimeHealthy`
列了 4 个值而其中 3 个永不可达。→ CHN-U3。

---

## 4. 错误信封

管理 API 统一返回 `{retcode, retmsg, data}`。前端 `src/api/client.ts:243-249` 把
`data.retcode` 塞进 `APIError.code`、`data.retmsg` 塞进 `.message`、`data.data` 塞进 `.details`。

### 4.1 控制面错误码（`ChannelControlError.error_code`）

| error_code | retcode | 语义 | 管理员该做什么 |
|---|---|---|---|
| `CHANNEL_NOT_ACCESSIBLE` | 109 AUTHENTICATION_ERROR | 渠道不属于你 | 换账号，或联系渠道所有者 |
| `INVALID_CHANNEL_CONFIGURATION` | 101 ARGUMENT_ERROR | 配置不完整/不合法（缺凭据、绑定缺失、版本过期、目标不可用） | 按 `retmsg` 补齐配置 |
| `CHANNEL_SECRET_STORE_UNAVAILABLE` | 105 CONNECTION_ERROR | 密钥库不可用 | **联系运维**，不是管理员能修的 |

⚠️ **这三个码今天到不了前端**：`_respond`（`api/apps/restful_apis/chat_channel_api.py:35-65`）
把 `data` 写死成 `False`，`error_code` 被丢弃；前端三处裸 `catch { toast.error(通用文案) }`
又把 `retmsg` 也丢掉。四类处置路径完全不同的失败被压成一句「渠道状态更新失败」。
→ CHN-U1（后端透出）+ CHN-U2（前端映射）。

### 4.2 运行时错误码（`last_error_code`）

由 worker 上报，受 `api/channels/runtime_client.py:16` 的 `^[A-Z0-9_]{1,64}$` 白名单约束，
因此**可安全外显**。**这份表不要手抄**——用下面这条命令重新枚举，新增码必须同步回本表：

```bash
grep -rhoE '(ChannelWorkerError|_request_stop|error_code=)\("?[A-Z][A-Z0-9_]{2,63}"?' \
  api/channels/ --include=*.py | grep -oE '[A-Z][A-Z0-9_]{2,63}' | sort -u
```

2026-08-05 实测结果（12 个）：

| error_code | 真实含义 | 处置方向 |
|---|---|---|
| `LEADER_LEASE_HELD` | 另一个 worker 已持有租约 | 通常是重复部署；⚠️ 也可能是跨租户抢占（CHN-S3 修） |
| `LEADER_LEASE_LOST` | 运行中丢失租约 | 同上 |
| `FEISHU_WS_STOPPED` | 飞书长连接断开 | 多半是 App Secret 被轮换或应用被停用。⚠️ 这个码由**传输层无关**的代码发出 → CHN-O4 改名为 `CHANNEL_TRANSPORT_STOPPED` |
| `FEISHU_CHANNEL_DISABLED` | 飞书侧应用被停用 | 去开放平台查应用状态 |
| `CHANNEL_NOT_SUPPORTED` | provider 名未注册 | 配置错误或版本不匹配 |
| `CHANNEL_RUNTIME_CONFIG_INVALID` | 私有 runtime 配置解析失败 | ⚠️ **多半是 tolerate/emit 顺序被违反**，见 [CHN-ADR-06](DECISIONS.md#chn-adr-06--私有-runtime-契约的每次变更都拆成-tolerate--emit-两个-pr) |
| `CHANNEL_RUNTIME_BINDING_INVALID` | binding 解析失败 | 同上 |
| `CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED` | 缺 internal token / runtime base URL | 运维补环境变量 |
| `CHANNEL_DEMO_MODE_UNSUPPORTED` | demo 模式不支持该操作 | — |
| `MANAGED_WORKER_FAILURE` | managed worker 通用失败 | 看服务端日志 |
| `REDIS_ADDRESS_INVALID` | Redis 地址配置错误 | 运维 |
| `REDIS_PREFLIGHT_FAILED` | Redis 预检失败 | 运维；Redis 不可用时 channel fail closed |

⚠️ 前端今天原样渲染这个大写码（`t('channel.runtime.errorCode', {code})`），没有任何 i18n 映射。
把它映射成「这是什么意思 + 你该做什么」是一次性成本，收益是把「联系研发」变成「自己能修」。

---

## 5. FieldSpec 渲染契约（CHN-P2 引入，v1 尚未存在）

服务端展平后的有序字段列表，前端**直接按序渲染，不做二次编译**。
JSON Schema（`config_schema`）仅用于服务端请求校验与 OpenAPI，**不下发给前端渲染**。
决策依据见 [CHN-ADR-03](DECISIONS.md#chn-adr-03--服务端展平-fieldspec前端不编译-json-schema)。

```
FormField:
  path: str          # 点号路径，"credential.app_id"
  kind: "text" | "password" | "string_list" | "select" | "switch"   # 开放联合
  label: str
  i18n_key: str|None
  required: bool
  secret: bool
  default / options / placeholder / help / max_items / max_length
```

**`kind` 是开放联合**：前端遇到不认识的 kind 必须渲染为 disabled 字段并显示 label，
**不得抛错**。这是老前端在服务端加入新控件类型时优雅降级的唯一保障。

⚠️ v1 里这个字段还不存在。前端当前从 `config_schema` 硬编码地推导飞书四个字段——
`channel.test.ts:68` 那条测试的名字自己就承认了：`'nested provider schema is flattened
for the Feishu form only'`。

---

## 6. v1 里编码了「错误行为」的三条断言

按 CHN-X1 的任务要求逐条标出。修这些条目时，**这些断言的语义要跟着改，不是绕过它们**。

**A. `channel.test.ts:68` — `'…flattened for the Feishu form only'`**
测试名本身就把飞书特例固化成了期望。它断言 `getProviderFields` 能从嵌套 `$ref` 里挖出
`app_secret` 并判定为 `secret`——但那条 `$ref` 解析路径**只在 `provider === 'feishu'` 分支里
被调用**。第二个 provider 走另一条分支，拿到的是根级 `{credential: {$ref}}`，
会渲染出一个明文输入框。→ CHN-P5/P6/P7 落地后，这条断言应改为「按服务端 `form.fields` 渲染」。

**B. `channel.test.ts:219` 与 `:252` — `assert.equal(putCalled, false)`**
把「绑定修改必须塞进 PATCH」固化成了契约。`PUT /{id}/binding` 这条为「只改绑定、不碰连接
配置」而存在的路由因此完全没有入口，改一次绑定要连带重发整个 `config`（含空的 `credential`）。
→ 将来接线 `putBinding` 时会先撞上这两条假失败。

**C. `channel.test.ts:251` — `updateRequest?.binding?.enabled === true`**
这个 `true` 来自 `baseInput.bindingEnabled`，而生产代码里它取自一个 staleTime 5 分钟的缓存。
断言本身没错，但它固化的是一个**无并发保护**的写路径：A 只改个渠道名保存，会把 B 刚停用的
渠道静默重新启用。→ CHN-U6 改成提交前 refetch 后，这条断言的取值来源要跟着改。

---

## 7. 已知的契约空白（v1 没有、但管理员需要）

| 空白 | 后果 | 归属 |
|---|---|---|
| supervisor 存活性没有任何信号 | supervisor 没跑时 `_serialize_runtime` 返回 `waiting`/`null`/`null`，**与「正在拉起」逐字节相同**。而 `docker/docker-compose.yml` 里压根没有 supervisor 服务——按默认方式部署，channel 功能 100% 不工作且 UI 一个字不说 | CHN-O5（先补 compose，这是主因）；存活信号的缝是 `ChannelRuntimeResponse` 上一个可空字段，随时可加 |
| `revision_stale` 只是提示，不是故障态 | 绑定的 Agent 发布新版本后，`executors.py:40-58` 会让**每条**消息失败，而管理页仍显示 `connected` / `last_error_code=null` | CHN-O1 |
| 保存前无法验证凭据 | 唯一验证路径是保存 → 启用 → 等一轮 reconcile → 读一个不透明错误码 | CHN-O6 |
| 无 binding 级指标 | 「机器人在线但不回消息」无法定位 | CHN-O9 |
| 无凭据变更审计 | 「谁在什么时候把这个渠道关了」答不出来 | CHN-O8 |

---

## 变更日志

| 日期 | 版本 | 变更 | 提交 |
|---|---|---|---|
| 2026-08-05 | v1 | 建立。从 `channel.test.ts` 的 11 条断言反推出现状契约；标出 3 处编码了错误行为的断言（§6）与 5 处契约空白（§7）；运行时错误码表由实测 grep 枚举（12 个），命令写在 §4.2 供重跑 | 待回填 |
