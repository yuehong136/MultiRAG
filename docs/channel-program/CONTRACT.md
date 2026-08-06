# Channel 前后端契约

> **契约版本**：`channel-api/v1` · **最后变更**：2026-08-05 · **变更提交**：`cdc09928`
>
> 本文件是 channel 前后端接口的**唯一真源**。前端仓不得保存第二份契约描述。
> 契约变更 = 改本文件 + 在本文件末尾的变更日志追加一行
> （见 [PROGRESS 维护协议第 4 条](PROGRESS.md#维护协议mandatory--任何处理本文档条目的-agent-必须遵守)）。
>
> **版本号按语义 bump，不是每次改动都 bump**：向后兼容的**加法**（新增字段、新增端点、
> 新增错误码取值）只记变更日志；**破坏性**变更（删字段、改字段含义、改必填性）才 bump
> `channel-api/vN` 并同步 `web:src/api/channel.ts` 的 `CHANNEL_API_VERSION`。
> 前者 bump 会让 CHN-X2 那条版本断言天天误报，反而训练出「红了就改常量」的习惯。
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
| `error` | 运行错误，看 `last_error_code` | **不一定来自 worker**：绑定启用且 Canvas 版本已过期时，由控制面读路径合成，见下方 |

**`error` 有两个来源，前端不必区分**（CHN-O1）。一个是 worker 上报的；另一个是控制面
在读路径上合成的：绑定已启用、但它锁定的 Canvas 版本不再是最新发布版时，`state` 报
`error`、`last_error_code` 报 `TARGET_REVISION_UNAVAILABLE`，**无视新鲜心跳**——执行层
对每一条消息都用同一个错误码拒绝，而 runner 本身确实活着、代次也对得上，新鲜心跳恰恰
是让这个故障隐形的东西。两条边界：runner 字段保留（进程是真的在跑）；绑定**未启用**时
不合成，`stopped` 才是真话。这个合成对私有运行时契约零影响（不动 `RuntimeReport`），
`GET /{id}/runtime` 与列表内嵌的 `runtime` 块**必须给出同一个答案**。

前端镜像在 `web:src/api/channel.ts` 的 `RUNTIME_STATES`，类型带 `(string & {})`
以便未知的新值仍能原样渲染而不是崩掉。`isRuntimeHealthy` 只认 `connected`。

历史：前端曾自建一份 12 条的词表，其中 `pending` / `running` / `healthy` / `online` /
`disabled` / `failed` 六个服务端永远不会返回，`isRuntimeHealthy` 列的 4 个值里 3 个
永不可达（结果碰巧正确，靠的是运气）。CHN-U3 已收敛，并在
`src/api/__tests__/channel.test.ts` 里加了断言把词表钉死。

---

## 4. 错误信封

管理 API 统一返回 `{retcode, retmsg, data}`。前端 `src/api/client.ts:243-249` 把
`data.retcode` 塞进 `APIError.code`、`data.retmsg` 塞进 `.message`、`data.data` 塞进 `.details`。

### 4.1 控制面错误码（`ChannelControlError.error_code`）

失败信封的 `data` 就是 `{"error_code": "..."}`（CHN-U1 起），前端 `APIError.details`
直接是它。前端映射在 `web:src/api/channel.ts` 的 `CHANNEL_ERROR_CODES` +
`channelErrorMessageKey`，文案在 `channel.errorCodes.*`（CHN-U2 起）。

| error_code | retcode | 出处 / 语义 | 管理员该做什么 |
|---|---|---|---|
| `CHANNEL_NOT_ACCESSIBLE` | 109 AUTHENTICATION_ERROR | `ChannelAccessDenied`：渠道不属于你 | 换账号，或联系渠道所有者 |
| `CHANNEL_TARGET_NOT_ACCESSIBLE` | 101 ARGUMENT_ERROR | `ChannelTargetNotAccessible`（CHN-S5）：看得见该目标，但无权把它发布到外部渠道 | 联系目标所属团队的管理员 |
| `INVALID_CHANNEL_CONFIGURATION` | 101 ARGUMENT_ERROR | `InvalidChannelConfiguration`：缺凭据、无绑定、版本过期、目标不可用、同账号已有启用渠道 | 按 `retmsg` 补齐配置 |
| `CHANNEL_SECRET_STORE_UNAVAILABLE` | 105 CONNECTION_ERROR | `ChannelCredentialUnavailable`：密钥库不可用 | **联系运维**，不是管理员能修的 |
| `CHANNEL_OPERATION_FAILED` | 100 EXCEPTION_ERROR | `_respond` 的兜底分支 | 联系运维并提供操作时间 |

兜底分支**也有码**，否则最可能到达管理员的那类失败反而是唯一没有可映射文案的。
新增码时两侧都要加：服务端的 `ChannelControlError` 子类，以及前端的
`CHANNEL_ERROR_CODES` 数组 + 两份 locale——前端对不认识的码回落到通用文案，
所以漏了不会崩，只会静默退化。

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
| `CHANNEL_TRANSPORT_STOPPED` | provider 长连接断开 | 多半是凭据被轮换或应用被停用。CHN-O4 已从 `FEISHU_WS_STOPPED` 改名——发出它的 `_monitor_channel` 监视的是 `Channel` 协议，与具体传输无关，旧名字在第一个非飞书 provider 上就是错的 |
| `FEISHU_CHANNEL_DISABLED` | 飞书侧应用被停用 | 去开放平台查应用状态 |
| `CHANNEL_NOT_SUPPORTED` | provider 名未注册 | 配置错误或版本不匹配 |
| `CHANNEL_RUNTIME_CONFIG_INVALID` | 私有 runtime 配置解析失败 | ⚠️ **多半是 tolerate/emit 顺序被违反**，见 [CHN-ADR-06](DECISIONS.md#chn-adr-06--私有-runtime-契约的每次变更都拆成-tolerate--emit-两个-pr) |
| `CHANNEL_RUNTIME_BINDING_INVALID` | binding 解析失败 | 同上 |
| `CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED` | 缺 internal token / runtime base URL | 运维补环境变量 |
| `CHANNEL_DEMO_MODE_UNSUPPORTED` | demo 模式不支持该操作 | — |
| `MANAGED_WORKER_FAILURE` | managed worker 通用失败 | 看服务端日志 |
| `REDIS_ADDRESS_INVALID` | Redis 地址配置错误 | 运维 |
| `REDIS_PREFLIGHT_FAILED` | Redis 预检失败 | 运维；Redis 不可用时 channel fail closed |

⚠️ 前端仍原样渲染这个大写码（`t('channel.runtime.errorCode', {code})`），没有 i18n 映射。
与 §4.1 的控制面错误码不同，这批还没做——把它们映射成「这是什么意思 + 你该做什么」
是一次性成本，收益是把「联系研发」变成「自己能修」。归 CHN-O 阶段。

---

## 5. FieldSpec 渲染契约（CHN-P2 起已下发）

服务端展平后的有序字段列表，前端**直接按序渲染，不做二次编译**。
JSON Schema（`config_schema`）仅用于服务端请求校验与 OpenAPI，**不下发给前端渲染**。
决策依据见 [CHN-ADR-03](DECISIONS.md#chn-adr-03--服务端展平-fieldspec前端不编译-json-schema)。

`GET /chat-channels/providers` 的每个 manifest 现在带 `form`：

```jsonc
{
  "provider": "feishu",
  "display_name": "Feishu / Lark",
  "capabilities": { "private_chat": true, "group_chat": false, ... },
  "form": {
    "version": 1,
    "fields": [
      { "path": "credential.app_id",     "kind": "text",        "label": "App ID",
        "i18n_key": "channel.fields.app_id", "required": true, "secret": false,
        "placeholder": "cli_xxxxxxxxxxxxxxxx" },
      { "path": "credential.app_secret", "kind": "password",    "label": "App Secret",
        "i18n_key": "channel.fields.app_secret", "required": true, "secret": true },
      { "path": "domain",                "kind": "select",      "label": "Domain",
        "required": true, "default": "feishu",
        "options": [ { "value": "feishu", "label": "Feishu (mainland)" },
                     { "value": "lark",   "label": "Lark (international)" } ] },
      { "path": "allowed_open_ids",      "kind": "string_list", "label": "Allowed open IDs",
        "max_items": 1000 }
    ]
  },
  "config_schema": { /* pydantic 生成，只服务校验与 OpenAPI */ }
}
```

规则：

1. **`kind` 是开放联合**。前端遇到不认识的 kind 必须渲染为 disabled 字段并显示 label，
   **不得抛错**——这是老前端在服务端加入新控件类型时优雅降级的唯一保障，也是推迟
   交互式配对（CHN-P12）的全部留缝成本。
2. **`required` 只在 form 里**。`config_schema` 的 `required` 数组是空的，而且会一直空：
   provider 的每个字段都带默认值，PATCH 才能是 merge 语义。前端曾为此硬编码一份
   required 集合，而且编码错了——schema 说全都可选。两处各自为真，就是这个设计。
3. **`secret: true` 的字段留空 = 保持不变**，不是清空。服务端永不回显密钥，所以
   「空」与「未改」在线格上不可区分，只能这么定义。
4. **前端按 `fields` 顺序渲染**，不排序、不解析 `$ref`、不求值任何 JSON Schema 关键字。
5. **提交时按 `path` 重组嵌套 config**，所以前端不需要知道任何 provider 的形状。
6. **`form.version` 只在破坏性变更时 bump**（CHN-X2 定死）。加字段、加 kind、加 option
   **不 bump**——未知 kind 已经渲染成 disabled、未知键本来就被忽略，加法零协调成本。
   bump 的含义是反过来的那半边：**不认识这个版本的客户端必须拒绝渲染，不许猜**。
   前端 `SUPPORTED_FORM_VERSION` 就是这条的执行者，把这种 manifest 丢进与「缺 form」
   同一条降级路径（横幅 + 禁用新建，列表/启停/删除照常）。所以 bump 一次 = 一次强制的
   跨仓部署顺序，别为了「更规范」而 bump。

服务端有四条一致性测试把 `form` 与 `config_model` 绑在一起
（`tests/unit/test_channel_provider_spec.py`）：每个 `path` 必须是模型真实接受的字段、
`secret=true` 的集合必须与 `secret_paths` 完全相同、select 必须有 options 且 default
在其中、path 不得重复。两份派生物因此不会漂移到互相矛盾。

**前端已消费**（CHN-P5/P6/P7 已落地，web `c294088` / `a09a09c`）：按 `form.fields`
渲染，客户端兜底 manifest 与飞书编译分支已删除，`listProviders` 丢弃缺 `form` 或
`form.version` 过高的 manifest，返回类型收窄为 `RenderableProviderManifest`。

---

## 6. v1 里编码了「错误行为」的三条断言（A 已解决，B/C 未解决）

按 CHN-X1 的任务要求逐条标出。修这些条目时，**这些断言的语义要跟着改，不是绕过它们**。

**A. `channel.test.ts:68` — `'…flattened for the Feishu form only'`**
测试名本身就把飞书特例固化成了期望。它断言 `getProviderFields` 能从嵌套 `$ref` 里挖出
`app_secret` 并判定为 `secret`——但那条 `$ref` 解析路径**只在 `provider === 'feishu'` 分支里
被调用**。第二个 provider 走另一条分支，拿到的是根级 `{credential: {$ref}}`，
会渲染出一个明文输入框。

**已解决（CHN-P7）**：这条断言连同 `getProviderFields` 一起删除。它值钱的那半边语义——服务端发来的密钥不得进入表单——由 `the form seeded from a stored channel never carries a secret` 继承（同一份 fixture，同一个 `must-not-reach-form` 哨兵值，但按服务端 `form.fields` 渲染，不再提飞书）；半态那半边由 `listProviders drops a manifest this client cannot render` 守住。

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
| 2026-08-05 | v1 | 建立。从 `channel.test.ts` 的 11 条断言反推出现状契约；标出 3 处编码了错误行为的断言（§6）与 5 处契约空白（§7）；运行时错误码表由实测 grep 枚举（12 个），命令写在 §4.2 供重跑 | cdc09928 |
| 2026-08-05 | v1（加法，不 bump） | 失败信封的 `data` 由 `False` 改为 `{"error_code": "..."}`（CHN-U1）；新增 `CHANNEL_TARGET_NOT_ACCESSIBLE`（CHN-S5）与兜底码 `CHANNEL_OPERATION_FAILED`。**向后兼容**：老前端只在成功路径读 `data`，失败路径读的是 `retcode`/`retmsg`，两者未变。按本文件头部的语义化规则，加法只记日志不 bump——这条规则本身是这次实测出来的，原先写的「契约变更就 bump」会让版本断言天天误报 | 86e76adc |
| 2026-08-05 | v1（消费侧，线格未变） | 前端接上了 §4.1 的错误码与 §3 的状态词表（CHN-U2/U3）。契约本身没变，只是两侧终于一致：§3 与 §4.1 里那批「前端还没消费 / 前端自建 12 条词表」的 ⚠️ 已按本文件规则清理，§4.2 的运行时错误码**仍未**做映射，与 §4.1 区分开并归入 CHN-O | web a2c98c0 |
| 2026-08-05 | v1（加法，不 bump） | manifest 新增 `form`（CHN-P2），§5 从「尚未存在」改写为实际下发的形状并给出完整 payload 示例。**向后兼容**：老前端忽略未知键；`config_schema` 一个字节没动，仍由 pydantic 生成、仍只服务校验与 OpenAPI。四条一致性测试把 `form` 与 `config_model` 绑住，防止两份派生物漂移 | 819e7ec2 |
| 2026-08-05 | v1（加法，不 bump） | `state="error"` 增加一个合成来源、`last_error_code` 增加一个取值 `TARGET_REVISION_UNAVAILABLE`（CHN-O1），§3 已写明。**向后兼容**：`error` 与 `last_error_code` 都是既有字段，老前端原样渲染即可；复用执行层已有的错误码而不是新造 `TARGET_REVISION_STALE`，是为了让面板和日志能 grep 同一个串。web 侧同批把 sheet 的 runtime 横幅补上原因行（卡片早就有） | 本次提交 + web `a752f3e` |
| 2026-08-05 | v1（规则澄清，不 bump） | 定死 `form.version` 的 bump 语义（§5 规则 6）：加法不 bump，破坏性才 bump，且 bump 的含义是「老客户端必须拒绝渲染」而非「尽力而为」（CHN-X2）。服务端 `ProviderForm.version` 原来的注释自相矛盾——一句说「只在客户端必须反应时 bump」，下一句说「未知的更高版本仍应渲染」，两条不能同时成立，已改写。前端落地 `CHANNEL_API_VERSION` 与 `SUPPORTED_FORM_VERSION` 两个常量并加断言，`listProviders` 按后者过滤 | 本次提交 + web `9873e25` |
| 2026-08-06 | v1（加法，不 bump） | 运行时错误码 `FEISHU_WS_STOPPED` → `CHANNEL_TRANSPORT_STOPPED`（CHN-O4），§4.2 已改。**不算破坏性**：`last_error_code` 一直是自由字符串（`str | None`，无 Literal），前端本来就原样渲染未映射的大写码，两侧都不需要协调 | 本次提交 |
| 2026-08-06 | v1（加法，不 bump） | `GET /chat-channels/providers` 现在返回**两个** manifest（`dingtalk`、`feishu`，按注册表顺序）。**这是本程序的核心验收，不是普通加法**：钉钉的 spec / transport / 注册全在后端，`git diff --stat` 里零个 `web/` 路径，前端不重新部署即可渲染并保存。四个字段用的都是既有 kind，所以 `form.version` 保持 1 | 本次提交 |
| 2026-08-06 | v1（加法，不 bump） | manifest 新增 `description` 与 `description_i18n_key`（CHN-P13），供客户端列出「还没接入的 provider」。**向后兼容**：两个字段都可选，老前端忽略；老后端不发时新前端渲染没有副标题的卡片，而不是渲染不出来 | `3a82e5f6` |
