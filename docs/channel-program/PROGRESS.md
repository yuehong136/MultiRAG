# Channel 程序 · 进度看板

## 维护协议（MANDATORY — 任何处理本文档条目的 agent 必须遵守）

1. **开工前**：用当前代码重新核实目标条目的锚点（文件、行号、符号）。本文档是时点快照，
   行号一定会漂移；以实际代码为准，**发现漂移先改锚点再动手**，不要带着错的锚点开工。
2. **完工后**：改任务表的 `状态`，并在「变更日志」追加一行，写清
   日期 / 做了什么 / 提交哈希 / **怎么验证的**。状态取值：
   `⬜ 未开始` / `🔵 进行中` / `✅ 完成` / `🚫 阻塞` / `⏸ 挂起` / `❌ 取消`。
   **「跑了 make verify」不算验证记录**——要写这条改动本身是怎么被证明成立的
   （新增的测试名、实测的 grep 结果、手动复现步骤）。
3. **范围变化**：发现新问题 → 按 §ID 规则**追加新 ID**，不要塞进无关条目；发现某条目不成立
   → 状态置 `❌ 取消` 并写明原因，**不要删行**（保留判断历史）。
4. **跨仓义务**：动到前后端契约的条目，必须在同一批工作里
   ① 更新 [CONTRACT.md](CONTRACT.md)（**按语义 bump**：加法只记变更日志，破坏性变更才 bump `channel-api/vN`）；
   ② 更新本文档「跨仓联动」表两侧状态与部署顺序；
   ③ 在 web 仓 `docs/engineering-modernization-roadmap.md` 对应条目补进展行。
   **只改一侧就宣布完成 = 未完成。**
5. **ID 出现在哪**：`CHN-<面><n>` 必须出现在提交标题的尾括号里；前端提交同时带上其路线图 ID，
   形如 `fix(channel): surface server error codes (ARCH-6, CHN-U2)`。
   `git log --grep=CHN-` 是本账本唯一的交叉校验手段——**ID 不进提交信息，账本就是废的**。
6. **硬不变量**（[README §6](README.md#6-硬不变量破坏之前先写-chn-adr)）任何条目都不得破坏。
   确需破坏 → 先写 `CHN-ADR-NN`，不允许在任务行里顺手改掉。

> 更新方式：改任务的 `状态` 列，在「变更日志」追加一行。

## ID 规则

```
CHN-<面><n>       任务    面 ∈ {S,U,P,O,X}，n 不补零，一个 ID 对应一个 PR
CHN-ADR-<nn>      决策    两位补零，追加式，永不复用
```

用 `CHN-` 而不是 `CH-`：`grep "CH-6"` 会命中 web 路线图的 `ARCH-6`（那份文档里有 21 处
`CH-` 子串）。`CHN-` 在两个仓零命中。

**不设里程碑命名空间**——`docs/feishu-multitenant/`（FMT）已经占了 `M0`–`M6`，第二套 `M1`
会让 `grep M1` 失效。所以**阶段即工作面字母**：S → U → P → O，X 贯穿。

| 面 | 含义 |
|---|---|
| **S** | 安全加固 |
| **U** | 今天就可见的缺陷（管理员现在就在踩） |
| **P** | Provider 通用化 |
| **O** | 运维能力与运行时诚实 |
| **X** | 跨仓契约 |

前端提交**双标**：既带路线图 ID（web 协议强制），也带 CHN ID，
`git log --grep=CHN-` 与 `git log --grep=ARCH-6` 都能还原真相。
web 侧 commit scope 从 `settings` 切到 `channel`（后端已有 `feat(channel):` 先例）。

---

## 阶段 S · 安全加固

全部纯后端，无跨仓依赖，**先做**。

| ID | 任务 | 状态 | 依赖 | 锚点 |
|---|---|:---:|---|---|
| CHN-S1 | 脱敏对齐：`_sanitize_public_config` 的精确匹配集合换成同文件 `_contains_sensitive_key` 已有的子串谓词。两个谓词分开——`credential` 在 config 里要**递归进去**（它含 `app_id`），在 `policy` 里要**整键拒绝** | ✅ | — | `api/channel_control/service.py:99-128` |
| CHN-S2 | desired-list 逐行降级：单条坏记录只跳过自己并记 `error_code`，不再让整轮 reconcile 停摆；`provider` 由 `Literal["feishu"]` 放宽为受约束的 `str`（**tolerate 半步**）。顺带修 `:495` `if secret is not None` 的静默过滤，补 `CHANNEL_SECRET_MISSING` 日志 | ✅ | — | `api/channel_runtime/schemas.py:15,38`、`api/apps/restful_apis/channel_runtime_api.py:41-49`、`api/channel_control/service.py:484-496` |
| CHN-S3 | Redis 命名空间改 **per-binding**：`RedisChannelStateStore(redis, *, scope: tuple[str,...])` 取代 `app_id: str`，`_KEY_PREFIX` → `multirag:channel:v2` | ✅ | — | `api/channels/state_store.py:105-124,238-245`、`api/channels/worker.py:366-373,455-462` |
| CHN-S4 | 同租户 provider 账号唯一性守卫（S3 之后两个 binding 会同时连上并重复回答）。**只在租户内**——全局唯一性检查本身就是跨租户抢占面 | ✅ | CHN-S3 | `api/channel_control/repository.py`（+`account_in_use`）、`service.py:787-798` |
| CHN-S5 | 目标授权：查询放宽到团队口径（与前端下拉同源），同时按**目标的归属租户**判 `can_update_tenant_resources`。新错误码 `CHANNEL_TARGET_NOT_ACCESSIBLE` | ✅ | — | `api/channel_control/repository.py:141-168`、`service.py:714-739` |
| CHN-S6 | 只读审计脚本，枚举会被 S5 拒绝的存量 binding 供运维处置 | ✅ | CHN-S5 | `scripts/audit_channel_target_authz.py` |

**CHN-S3 为什么是 binding 维度而不是租户维度**：worker 手上根本没有 `tenant_id`——
`RuntimeBindingConfig` 不携带它。租户维度需要往 `extra="forbid"` 的私有契约模型加字段，
把一个必须**现在**上线的安全修复变成两次部署的协议升级。`binding_id` 已在手、全局唯一、
按构造即租户隔离，还顺带白送修掉「同 `app_id` 重建渠道复用 dedupe 命名空间导致静默丢消息」。
**已知代价**（要写进 PR body 与 `api/channels/README.md`）：接住这次改动的那一次 worker 重启，
会话重置一次、dedupe 窗口空一次。

**CHN-S5 为什么不是「九条路由补 `can_manage_*`」**：`UserTenantService.get_role_in_tenant`
（`api/db/services/user_service.py:289-290`）是 `if user_id == tenant_id: return OWNER`，
而 channel 路由传的正是 `Principal.id`——那个检查恒为 True，是纯表演。详见 `CHN-ADR-01`。

---

## 阶段 U · 今日可见缺陷

| ID | 仓 | 任务 | 状态 | 依赖 | 锚点 |
|---|---|---|:---:|---|---|
| CHN-U1 | MR | `_respond` 把 `data=False` 换成 `data={"error_code": ...}`。`retcode`/`retmsg` 不动，前端 `APIError.details` 零改动就能拿到 | ✅ | — | `api/apps/restful_apis/chat_channel_api.py:35-65` |
| CHN-U2 | WEB | 错误码接线（三处裸 `catch` 改为按 code 映射文案）+ providers 查询失败不再清空整页，改内联横幅 + 禁用新建，渠道列表照常渲染 | ✅ | — | `src/api/channel.ts`、`index.tsx:44-47,67-69,78-80,97-106`、`channel-form-sheet.tsx:140-142`、两份 locale |
| CHN-U3 | WEB | 运行时状态词表对齐：`state` 收紧为 6 值联合 + `(string & {})`；`isRuntimeHealthy` 变成 `=== 'connected'`；删 6 个服务端永不上报的 locale 条目；**把测试从 `pages/.../__tests__/` 移到 `src/api/__tests__/`**（前者跑在所有门禁之外） | ✅ | — | `src/api/channel.ts:56`、`utils.ts:10-13`、两份 locale |
| CHN-U4 | WEB | 表单重置守卫：按 `currentChannel?.id` 而非对象引用触发，`isDirty` 时不重置；detail 查询关掉 `refetchOnReconnect`/`refetchOnWindowFocus`。修「网络抖动静默清空已输入的 App Secret」 | ✅ | — | `channel-form-sheet.tsx:89-92`、`use-channel-request.ts:29-34` |
| CHN-U5 | WEB | `setQueryData` 改 `invalidateQueries`——mutation 响应不带 runtime，写进读缓存会抹掉实时面板 | ✅ | — | `use-channel-request.ts:71-75,83-89` |
| CHN-U6 | WEB | 提交前 `fetchQuery` 拿新鲜的 `binding.enabled`，不用 5 分钟旧缓存。修「A 改个名把 B 刚停用的渠道静默重新启用」 | ✅ | — | `channel-form-sheet.tsx:114-143` |
| CHN-U7 | WEB | 绑定下拉服务端搜索（`useFetchAgentList` 已支持 `keywords`），去掉硬编码 `page_size: 100` | ✅ | — | `binding-fields.tsx:37-44` |

**CHN-U6 为什么是 refetch 而不是省略字段或加后端令牌**：省略 `enabled` 时老后端会读到
`ChannelBindingUpsertRequest.enabled` 的 `False` 默认值 → 静默**停用**渠道，这是坏的半态；
加后端令牌会为一个亚秒级竞态制造硬跨仓顺序约束。refetch 是纯前端、对任何后端版本都安全，
把窗口从 5 分钟压到一次往返。残余竞态已知并接受。

---

## 阶段 P · Provider 通用化

**验收标准：钉钉落地时 `git diff --stat` 里零个 `web/` 路径。**

| ID | 仓 | 任务 | 状态 | 依赖 | 锚点 |
|---|---|---|:---:|---|---|
| CHN-P1 | MR | 新建 `api/channel_providers/` 纯规格包（无 ORM / 无 web 框架 / 无 SDK）+ 飞书 spec；**删除死注册表** `api/channels/core/registry.py`；加 import-linter 契约 + 子进程纯度测试 | ✅ | — | 新包、`channel_control/schemas.py`、`pyproject.toml` |
| CHN-P2 | MR | 发 `manifest.form`——服务端展平的有序 FieldSpec。`config_schema` 原样保留，继续只服务请求校验 + OpenAPI | ✅ | CHN-P1 | `api/channel_providers/base.py`、`feishu/spec.py`、`channel_control/schemas.py:199-223` |
| CHN-P3 | MR | 配置/凭据拆分改由 spec 驱动，消掉 5 个硬编码 `app_id`/`app_secret` 门里的 4 个 | ✅ | CHN-P1、CHN-P2、CHN-S4 | `service.py:131-173,498-531,787-798` |
| CHN-P4 | MR | `RuntimeCredential.fields` — **tolerate 半步**（API 不发新字段） | ✅ | — | `api/channel_runtime/schemas.py:25-31`、`channels/feishu/provider.py:75-93` |
| CHN-P5 | WEB | 新建 `form-spec.ts`：`resolveFormFields`（有 `manifest.form` 用它，否则回落到既有飞书编译分支——**前端的 tolerate 步**）+ `assembleConfig`（走点号路径，空 secret 字段整个省略）。`buildChannelMutationPayload` 改 spec 驱动。**纯函数，不碰组件** | ✅ | CHN-P2（软） | `src/pages/settings/channels/form-spec.ts`、`src/api/channel.ts:85-179` |
| CHN-P6 | WEB | UI 接线到 spec；**修死表单**（zod 改用 `selectedManifest` 而非 `providers[0]`）；`ChannelFormValues.config` 改成能表达嵌套与布尔的类型；拆出 runtime banner 与 basics section 把 `channel-form-sheet.tsx` 从 310 行降到 250 以下 | ✅ | CHN-P5、CHN-U2 | `form-model.ts:144-262`、`provider-fields.tsx`、`channel-form-sheet.tsx` |
| CHN-P7 | WEB | 删客户端兜底 manifest 与飞书编译分支。**半态修复写在这个 PR 内部**：`listProviders()` 丢弃缺 `form` 的 manifest | ✅ | CHN-P5、CHN-P6、**CHN-P2 已部署** | `form-model.ts:71-142`、`index.tsx:44-47` |
| CHN-P8 | MR | `RuntimeCredential.fields` — **emit 半步**，同时保留 legacy 字段对 | ✅ | **CHN-P4 已部署** | `channel_runtime_api.py:72-81` |
| CHN-P9 | MR | `ChannelProvider` 改注册表驱动；`config` 改 `dict[str, Any]` 在 service 层按 `channel` 判别后二次校验。**这是 CHN-S2 的 emit 闸门** | ✅ | **CHN-S2 已部署**、CHN-P1、CHN-P3、**CHN-P8 已部署** | `channel_control/schemas.py:10,99-136,172-188` |
| CHN-P10 | MR | **钉钉 provider**（`credential.client_id` + `credential.client_secret`）。零前端改动的验收 PR | ✅ | CHN-P9 | 新 spec + transport |
| CHN-P13 | MR+WEB | provider **可发现性**：manifest 加 `description` / `description_i18n_key`；前端把「一个新建按钮 + 抽屉里的下拉」改成「已接入 / 可接入」两段，可接入是服务端驱动的卡片画廊 | ✅ | CHN-P10 | `channel_providers/spec.py`、`web:components/provider-gallery.tsx` |
| CHN-P11 | MR | 删除 legacy `RuntimeCredential.app_id/app_secret`（浸泡后清理，删字段三步的第三步） | ⬜ | CHN-P8 已浸泡 | `api/channel_runtime/schemas.py` |
| CHN-P12 | — | ⏸ 交互式配对（QR / OAuth）。**不做**，但 `FormField.kind` 保持开放联合、前端渲染未知 kind 为 disabled，就是它的全部留缝成本 | ⏸ | — | — |

**CHN-P2 的 FormField 形状**（这是解开渲染契约僵局的一步——`required` 落在 form 层，
`FeishuConfigInput` 因此名正言顺地保持全字段可选以支持 PATCH merge）：

```
FormField:
  path: str          # 点号路径，"credential.app_id"
  kind: "text" | "password" | "string_list" | "select" | "switch"   # 开放联合
  label: str         # 服务端英文默认值
  i18n_key: str|None # 让既有 locale 继续赢
  required: bool     # 真正的答案（JSON Schema 里没有 required 数组）
  secret: bool       # 留空 = 保持不变
  default / options / placeholder / help / max_items / max_length
```

`kind` 必须是**开放联合**，前端渲染未知 kind 为 disabled 字段而非抛错——这是将来加企微
`visible_when`、加 OAuth 按钮时老前端能优雅降级的唯一保障。**第二个 provider 已定为钉钉**
（两个字段，现有 kind 枚举即可表达），所以 CHN-P2 不做条件可见性；但 fixture 里要同时放
飞书与钉钉两份，把「零前端改动」在设计期就验证掉。

**CHN-P1 的分层约束**（`api/channels/feishu/__init__.py:1` 会 eager import lark-oapi，
所以 spec 绝不能放在 `api/channels/<name>/`）：

```toml
[[tool.importlinter.contracts]]
name = "channel_providers 是纯规格层：不依赖 ORM/路由/传输实现"
type = "forbidden"
source_modules = ["api.channel_providers"]
forbidden_modules = ["api.db", "api.apps", "api.channels", "api.channel_control", "api.channel_runtime"]
```

import-linter 表达不了「不许第三方 SDK」，所以补一个子进程测试：`import api.channel_providers`
之后 `sys.modules` 里不得出现 `lark_oapi` / `sqlalchemy` / `fastapi`。

---

## 阶段 O · 运维能力与运行时诚实

| ID | 任务 | 状态 | 依赖 | 锚点 |
|---|---|:---:|---|---|
| CHN-O1 | `revision_stale` 升级为**故障态**：stale 时报 `state="error"` + `last_error_code="TARGET_REVISION_STALE"`，无视新鲜心跳。`get_runtime` 也要算这个标志——今天只有 `_serialize_channel` 算，专用 runtime 路由**更**不准。**零私有契约影响**（控制面读路径合成） | ✅ | — | `service.py:438-453,579-591,662-700` |
| CHN-O2 | `RuntimeBindingConfig.policy` — **tolerate 半步**。今天 `policy.private_chat_only` 被表单收集、被服务端校验、被存进 DB，然后**永远到不了 worker**，管理端那个开关是装饰品 | ✅ | — | `api/channel_runtime/schemas.py:34-41`、`binding_bridge.py:74-76` |
| CHN-O3 | `policy` — **emit 半步** | ✅ | **CHN-O2 已部署到所有 worker** | `service.py:498-531`、`channel_runtime_api.py:72-81` |
| CHN-O4 | worker 传输层无关化：`FEISHU_WS_STOPPED` → `CHANNEL_TRANSPORT_STOPPED`；`:472` 不再硬编码 `FeishuBindingBridge`；`chat_type != "p2p"` 过滤移到 policy + provider 能力后面 | ✅ | CHN-O3、CHN-P1 | `worker.py:322-329,472-479`、`binding_bridge.py` |
| CHN-O5 | **supervisor 进 `docker/docker-compose.yml`**。今天 compose 只有 `multirag-cpu`/`multirag-gpu`，默认部署下 channel 功能 100% 不工作且 UI 一个字不说。刻意排在 O2/P4 之后——第一次跑起来的 supervisor 就已经在最新契约上 | ✅ | CHN-P4、CHN-O2 在镜像里 | `docker/docker-compose.yml`、`api/channels/README.md:405-433` |
| CHN-O6 | 连接自检端点（保存前验证凭据，把数十秒的反馈环压到 2 秒） | ⬜ | CHN-U1 | 未排期 |
| CHN-O7 | 主密钥 keyring 读侧（今天丢钥 = 全租户凭据永久不可解密，且无回退路径） | ⬜ | — | 未排期 |
| CHN-O8 | 凭据变更审计轨迹 | ⬜ | — | 未排期 |
| CHN-O9 | binding 级可观测（消息量、丢弃原因、时延分位） | ⬜ | — | 未排期 |
| CHN-O10 | 自适应轮询（**SSE 已否决**，见 `CHN-ADR-02`） | ⬜ | — | 未排期 |
| CHN-O11 | 渠道数配额 | ⬜ | — | 未排期 |

---

## 阶段 X · 跨仓契约

| ID | 任务 | 状态 | 锚点 |
|---|---|:---:|---|
| CHN-X1 | 从 `web:src/api/__tests__/channel.test.ts` 已有的断言反推出 [CONTRACT.md](CONTRACT.md)，标出其中编码了「今天的错误行为」而非「意图行为」的那几条 | ✅ | `CONTRACT.md` |
| CHN-X2 | `channel-api/vN` 版本标记落地 + web 侧 5 行断言（唯一的工具预算） | ✅ | `web:src/api/__tests__/channel.test.ts` |
| CHN-X3 | 端到端联调验收：钉钉注册后，CHN-P7 那次构建出来的前端不重新部署就能渲染并保存 | ✅ | — |
| CHN-X4 | ⏸ Go 侧 channel。**不做**——JSON 形状本身就是缝，且已被下面的规则版本化 | ⏸ | — |

---

## 跨仓联动

> 部署顺序取值：`后端先` / `同批` / `前端先（禁止）`。
> 「后端 ✅ + 前端 🔵」是正常中间态，必须写清兼容窗口何时关闭。

| CHN ID（后端） | 状态 | CHN ID（前端） | 状态 | web 路线图 ID | 部署顺序 | 兼容窗口 / 备注 |
|---|:---:|---|:---:|---|---|---|
| CHN-U1 | ✅ | CHN-U2 | ✅ | ARCH-6 | 后端先 | 前端可先落地（无 `error_code` 时回落到今天的通用文案），后端补齐后自动变准 |
| — | — | CHN-U3 | ✅ | ARCH-6 | 无依赖 | 服务端从今天起就只发 6 个状态，前端删幽灵条目不需要等后端 |
| — | — | CHN-U6 | ✅ | SEC-4 | 无依赖 | 刻意做成纯前端，见阶段 U 的说明 |
| CHN-P2 | ✅ | CHN-P5 | ✅ | ARCH-6 | 后端先 | manifest 加 `form` 是加法，老前端忽略即可。**兼容窗口已于 CHN-P7 关闭** |
| CHN-P2 | ✅ | CHN-P7 | ✅ | ARCH-6 | **后端先（硬依赖，已满足：CHN-P2 = `819e7ec2`）** | 全程序唯一一条真跨仓依赖。半态已设计成「降级且可读」：老后端 → `listProviders` 过滤空 → 横幅提示 + 禁用新建，列表/启停/删除照常 |

---

## 待办任务简报（零上下文可直接开工）

上面的任务表是一句话索引；这一节是**交接用的执行简报**。每条都给了「问题是什么（带证据）
/ 为什么值得做 / 闸门 / 验收标准 / 从哪读起」。**开工前仍按维护协议第 1 条复核锚点**——
行号一定会漂。

### CHN-P11 · 删掉 legacy `RuntimeCredential.app_id/app_secret`

- **状态**：⬜ 等浸泡。**这是唯一一条「等时间」而不是「等人」的任务。**
- **问题**：删字段三步的第三步。① 停止读 = CHN-P4（已部署）② 停止发 = CHN-P8（已部署）
  ③ 删除 = 本条。`api/channel_runtime/schemas.py::RuntimeCredential` 上那对字段是飞书的
  命名，第二个 provider 一旦去够它们就等于把刚拆掉的耦合请回来（`dingtalk/provider.py`
  已经**只读** `value("client_id")`，没有 legacy 回退，可作范本）。
- **闸门（两条都要满足，缺一不可）**：
  1. 线上**所有** worker 都跑在含 CHN-P8 的构建上。worker 是 supervisor spawn 时从盘上
     加载的，所以「重启 supervisor」通常就够；但如果别处还有独立跑的 runner，要一并确认。
     查法见 README §3 的「先查，别假设」。
  2. **API 进程也要重启到含本条的构建**。方向和上面相反：删字段后**老 API 仍在发**
     legacy 那一对，而新 worker 的 `extra="forbid"` 会整包拒绝。
- **改哪里**：`api/channel_runtime/schemas.py`（删两个字段，`value()` 的 `legacy` 参数
  一并删）、`api/apps/restful_apis/channel_runtime_api.py`（构造 `RuntimeCredential` 时
  不再传）、`api/channels/feishu/provider.py`（`value("app_id", legacy=...)` → `value("app_id")`）。
- **验收**：`test_runtime_config_releases_only_provider_connection_material_to_authenticated_runner`
  的线格断言里那两个键消失，且**其余断言一字不改**；`test_runtime_credential_tolerates_both_contract_halves`
  按新语义重写（不再有 legacy 一侧）；`-k "channel or feishu or dingtalk"` 全绿。
- **别做的事**：不要顺手给 `fields` 加校验或改成 `extra="allow"`——那是另一次契约变更。

### CHN-O6 · 连接自检端点

- **问题**：今天填错 App Secret 的反馈环是**几十秒起步**：保存 → 启用 → 等 supervisor
  下一轮 reconcile（默认 10s）→ worker 起来 → 握手失败 → 上报 → 前端轮询（15s）拿到
  `error`。中间任何一步慢一点，管理员看到的都是「转圈然后不知道为什么不行」。
- **值得做的理由**：这是这个页面**唯一**一个「用户做对了事却要等很久才知道」的地方，
  其余错误（缺字段、目标不可访问）都已经是同步返回。
- **设计要点**：
  - 端点形如 `POST /chat-channels/{id}/verify`（对已存渠道，用存着的密钥，**请求体不带
    凭据**）。对「还没保存」的场景不要做——那需要把明文凭据放进一个新的请求体，
    多一个凭据入口就多一处泄漏面，收益不抵。
  - 传输层要暴露一个「只认证、不建长连接」的能力。飞书可以调一次 `tenant_access_token`；
    钉钉可以只做 `connections/open` 拿到 ticket 就断开（`api/channels/dingtalk/channel.py::_open_connection`
    已经是独立方法，可直接复用）。**建议在 `WorkerProvider` 上加一个可选的
    `verify_credential()`**，没实现的 provider 返回「不支持自检」而不是报错。
  - **必须限流**，否则这是一个用别人租户的凭据去打第三方 API 的放大器。
- **闸门**：无。纯加法，老前端不调用即可。
- **验收**：错误凭据在 2 秒内返回带 `error_code` 的失败信封；正确凭据返回成功；
  日志与响应体里都搜不到凭据值。

### CHN-O7 · 主密钥 keyring 读侧（**风险最高的一条**）

- **问题（已核实）**：`api/channel_control/secret_store.py::AESGCMChannelSecretStore.decrypt`
  的第一行判断是 `encrypted.key_id != self._cipher.key_id → SecretStoreUnavailable`。
  也就是说**只有一把活跃密钥**：换掉 `channels.control.secret_encryption_key`，全部租户
  已存的凭据立刻永久不可解密，**没有回退路径**，只能让每个管理员重新填一遍。
- **值得做的理由**：这不是功能缺失，是一个**没有安全出口的运维陷阱**。密钥泄漏时正确的
  反应是轮换，而今天轮换的代价等于全量凭据丢失，等于「泄漏了也不敢换」。
- **设计要点**：`EncryptedSecret.key_id` **已经存在并已随密文一起存**，所以读侧改造是
  纯加法：配置从一把密钥变成一个 `key_id -> key` 的映射（外加一个 `active_key_id`），
  `decrypt` 按密文自带的 `key_id` 选密钥，`encrypt` 永远用 active。**先只做读侧**，
  重加密（把旧密文迁到新密钥）单独一条，不要合在一起。
- **闸门**：无。配置向后兼容（单密钥写法要继续能用）。
- **验收**：用旧密钥加密的密文在配置了新 active 密钥后**仍能解密**；移除某个 key_id 后
  对应密文报 `CHANNEL_SECRET_STORE_UNAVAILABLE` 而不是静默返回空；测试覆盖「两把密钥
  并存」这一种状态。

### CHN-O8 · 凭据变更审计轨迹

- **问题**：谁在什么时候换了哪个渠道的密钥，今天查不到。`ChannelSecret.version` 只告诉你
  换过几次。
- **设计要点**：只记**事实**不记**内容**——租户、渠道、principal、动作、时间、新版本号。
  绝不记密钥前缀、长度、哈希以外的任何派生物。
- **闸门**：无。

### CHN-O9 · binding 级可观测

- **问题**：消息量、丢弃原因（`allowed_sender_ids` 拒绝 / 群聊被 policy 拒 / 去重命中）、
  时延分位，今天全部只存在于日志行里，没有聚合。
- **提示**：丢弃分支已经都带结构化 `error_code`，做聚合不需要改业务代码，只需要一个计数器
  出口。

### CHN-O10 · 自适应轮询（**SSE 已否决，先读 CHN-ADR-02**）

- **问题**：`web:src/hooks/use-channel-request.ts` 的 `refetchInterval: 15 * 1000` 是固定的，
  渠道停用、页面失焦时照样打。
- **设计要点**：状态是终态（`stopped` / 无 binding）时停止轮询；`starting` 这类过渡态可以
  更密；页面不可见时暂停。**不要做 SSE**——心跳 15s、reconcile 10s、状态存在 Postgres 且
  无 pub/sub，SSE 端点只能自己在服务端轮询那张表，把一次轮询换成两次。

### CHN-O11 · 渠道数配额

- **设计要点**：落点是创建路径上的一次 count + 一个配置值，任何时候都是纯加法。
- **闸门**：无。

### 还未排期但已知的两条

- **CHN-P12 · 交互式配对（扫码 / OAuth）**：⏸ 明确不做。留缝成本已经付过了——
  `FormField.kind` 是开放联合、前端渲染未知 kind 为 disabled，将来只是多一个 kind 值
  加一条回调路由。
- **CHN-X4 · Go 侧 channel**：⏸ 明确不做。JSON 形状本身就是缝，且已被
  [CHN-ADR-06](DECISIONS.md) 版本化。要守的是 `api/channel_providers/` 保持纯数据 +
  pydantic（import-linter 契约与子进程纯度测试已强制）。

---

## 部署顺序矩阵

两个仓独立部署，**每一行都必须在两种半态下都不坏**。上一轮设计正是因为缺这张表被评审否决。

| CHN ID | 仓 | 后端已上线、前端未上线 | 前端已上线、后端未上线 | 坏? |
|---|---|---|---|:---:|
| S1 | MR | `GET /chat-channels` 不再返回 `client_secret` 等；前端只读 `credential.app_id`/`domain`/`allowed_open_ids`，都不匹配该谓词 | n/a | 否 |
| S2 | MR | 无可见变化。原本会 500 整条私有路由、跳过整轮 reconcile 的坏行，现在只跳过自己 | n/a | 否 |
| S3 | MR | worker 重启后如常 `connected`；会话重置一次、dedupe 窗口空一次（已知代价） | n/a | 否 |
| S4 | MR | 同租户内启用第二个同 app 渠道会失败并弹通用文案（U2 之前）；存量已启用的不受影响 | n/a | 否 |
| S5 | MR | 团队共享的 Agent 之前出现即被拒，现在对 OWNER/ADMIN **成功**；NORMAL 成员看到与今天一样的通用失败 | n/a | 否 |
| S6 | MR | 无可见变化 | n/a | 否 |
| U1 | MR | 无可见变化——前端还是裸 catch，没人读 `data` | n/a | 否 |
| U2 | WEB | n/a | 老后端不给 `error_code`，映射函数回落到今天的通用文案；providers 失败改横幅是任何后端下的严格改进 | 否 |
| U3 | WEB | n/a | 服务端从来就只发这 6 个；未知值仍由 `defaultValue` 原样渲染 | 否 |
| U4 | WEB | n/a | 纯本地表单行为 | 否 |
| U5 | WEB | n/a | 每次 mutation 多一个 GET；保存后 runtime 面板不再变空 | 否 |
| U6 | WEB | n/a | 提交前多一个 GET；`enabled` 仍按 bool 发送，`status`/`enabled` 一致性校验与今天一样通过 | 否 |
| U7 | WEB | n/a | `keywords` 是 agent 列表路由本来就接受的参数 | 否 |
| P1 | MR | 无可见变化——`provider_manifests()` 输出逐字节相同 | n/a | 否 |
| P2 | MR | 无可见变化——多一个 JSON 键，现有前端忽略 | n/a | 否 |
| P3 | MR | 无可见变化——行为保持，由未修改的既有测试证明 | n/a | 否 |
| P4 | MR | 无可见变化。API 仍只发 legacy 字段对。**这个窗口里必须重启 supervisor** | n/a | 否 |
| P5 | WEB | n/a | 有 `form` 用 `form`，没有就走 legacy 编译分支 → 对老后端逐字节相同的渲染 | 否 |
| P6 | WEB | n/a | 同上回落。多 provider 提交死锁在两种后端下都被修好 | 否 |
| **P7** | WEB | n/a | **唯一真依赖**。老后端 → `listProviders` 丢弃无 `form` 的 manifest → providers 为空 → U2 的横幅「provider 不可用、新建已禁用」，而列表/卡片/启停/删除全部照常。降级且可读，不是静默 | 否（靠设计） |
| P8 | MR | 无可见变化。legacy 字段对仍在发，老 worker 照样能解析 | n/a | 否 |
| P9 | MR | 无可见变化，直到第二个 spec 存在 | n/a | 否 |
| P10 | MR | provider 下拉多一项且表单能渲染——**用的是 P7 那次构建的前端，不重新部署** | n/a | 否 |
| P11 | MR | 无可见变化。任何早于 P4 的 worker 会停止解析凭据 → 那条 binding 报 `RUNTIME_CONFIG_INVALID`。闸门是浸泡时间 | n/a | 否（遵守规则的前提下） |
| O1 | MR | 卡片与面板对 stale binding 显示 **运行错误** + `TARGET_REVISION_STALE`，而不是虚假的 `connected`。U3 之前的前端也能渲染（`error` locale 条目今天就有） | n/a | 否 |
| O2 | MR | 无可见变化。API 不发新字段。**这个窗口里必须重启 supervisor** | n/a | 否 |
| O3 | MR | 「仅私聊」开关终于生效。若还有早于 O2 的 worker 在跑，**那条 binding 起不来**——`extra="forbid"` → `RUNTIME_CONFIG_INVALID` → 管理员看到 `waiting` 且无错误码。**这正是下面那条规则存在的理由，闸门是流程不是代码** | n/a | 否（遵守规则的前提下） |
| O4 | MR | 新的 WS 断开报 `CHANNEL_TRANSPORT_STOPPED` 而非 `FEISHU_WS_STOPPED`；两者都原样渲染，老行保留老码 | n/a | 否 |
| O5 | MR | 默认部署下 channel 第一次真正工作起来；原本永远 `waiting` 的渠道会走到 `starting` → `connected` | n/a | 否 |

**被这张表逼着改掉的六条 PR 边界**（每一条都替换掉了一个坏半态）：

1. **S3 改成 binding 维度**——租户维度要往 `extra="forbid"` 的私有模型加字段，把紧急安全修复变成两次部署的协议升级。
2. **U6 改成提交前 refetch**——省略 `enabled` 会让老后端读默认 `False` 静默停用渠道。
3. **P7 的 `listProviders` 过滤掉无 `form` 的 manifest**——否则对老后端会渲染出零字段但保存按钮可点的表单。
4. **P5/P6 保留 legacy 编译分支**，删除单独放 P7——否则消费 PR 硬依赖 P2 已部署。
5. **O1 做成控制面读路径合成**，不加 `RuntimeReport` 字段——否则 worker 得懂 Canvas 发布，且要两次部署。
6. **P4/P8 与 O2/O3 各拆成 tolerate/emit 对**，中间强制一次 worker 部署。

---

## 私有运行时契约升级规则

`DesiredRuntime` / `DesiredRuntimeList` / `RuntimeCredential` / `RuntimeBindingConfig` /
`RuntimeReport`（全在 `api/channel_runtime/schemas.py`）都是 `extra="forbid"`，
而 supervisor 与 worker 是长驻进程、**API 部署不会重启它们**——`docker/docker-compose.yml`
里压根没有 supervisor 服务（CHN-O5 才补上）。所以两侧永远假定在不同的提交上。

> **这五个模型的每一次变更都拆成两个 PR，中间夹一次运行时部署。教会*消费方*接受新形状的
> 那个 PR 先合并并部署到位（tolerate），之后让*生产方*发出它的 PR 才能合（emit）。
> 一个 PR 同时做两件事就是协议破坏，不管那个字段看起来多「加法」——`extra="forbid"`
> 会把未知键变成整次调用的解析失败，而对 `DesiredRuntimeList` 来说那意味着一个 binding
> 都不会被 reconcile。**

方向决定谁先动：

| 模型 | 生产方 | 消费方 | 谁先动 |
|---|---|---|---|
| `DesiredRuntime` / `DesiredRuntimeList` | API | supervisor | 消费方 |
| `RuntimeBindingConfig` / `RuntimeCredential` | API | worker | 消费方 |
| `RuntimeReport` | worker | API | 消费方（即 API） |

- **放宽 `Literal` 也是 tolerate-then-emit**，只是发生在值域：`DesiredRuntime.provider`
  在 CHN-S2 放宽，但第一条非 feishu 行要等 CHN-P9。
- **删字段是三步**：先停止读（消费方，部署）→ 停止发（生产方，部署）→ 删。CHN-P11 是第三步。
- **能在控制面合成的语义变更豁免**（无 schema 变更），CHN-O1 就是刻意这么做的。

跳过 tolerate 的实际后果，**每个 emit PR 的 release note 必须写明它对应的 tolerate PR 与最低运行时版本**：

| 对 | 跳过会看到什么 |
|---|---|
| `policy`（O2 → O3） | worker `fetch_binding` 抛 `RUNTIME_CONFIG_INVALID` → 在报任何状态之前退出 → `_serialize_runtime` 给出 `waiting`/`null`/`null`，**与「正在启动」逐字节相同**。全子系统最坏的失败模式 |
| `provider` 放宽（S2 → P9） | `supervisor.py:96-101` 跳过**整轮** tick，健康的飞书 binding 也一起停止被 reconcile 和回收 |
| `credential.fields`（P4 → P8） | P8 仍在发 legacy 字段对，所以这一对不会立刻咬人；闸门在 **CHN-P11** 才变硬 |

**一条能缩小整个问题的运维事实**：CHN-O5（supervisor 进 compose）刻意排在 P4 与 O2 之后。
对任何今天没跑 supervisor 的部署——按 compose 现状那是**默认情况**——它跑起来的第一个
supervisor 就已经越过了两个 tolerate 步。这条规则因此只约束当前手工运行 supervisor 的少数环境。

---

## 复盘节奏

| 时点 | 动作 |
|---|---|
| 每个阶段结束 | 跑下面两条 git log 对账，补漏记的提交；关闭该阶段全部条目 |
| 阶段 S 结束 | 更新 `api/channels/README.md` 的「尚未实现或不能宣称」清单，删掉已实现项 |
| 阶段 U 结束 | 核对 CONTRACT 的状态词表与错误码表和前端实际渲染是否一致 |
| 阶段 P 结束 | bump `channel-api/v2`；确认 web 侧 `CHANNEL_API_VERSION` 已跟上 |
| 阶段 O 结束 | README §6 硬不变量逐条实测复核 |
| 每次跨仓条目完成 | 两侧账本都更新了才算完成（协议第 4 条） |

**对账用的两条命令**（第二条里有、第一条里没有的提交，就是漂移）：

```bash
# 账本建立于 cdc09928，起点边界不能省——否则会把建账本之前的全部历史都报成漂移
git log --grep=CHN- --oneline cdc09928..
git log --oneline cdc09928.. -- api/channels api/channel_control api/channel_execution api/channel_runtime
```

差集一行命令：

```bash
comm -13 <(git log --grep=CHN- --format=%H cdc09928.. | sort) \
         <(git log --format=%H cdc09928.. -- api/channels api/channel_control api/channel_execution api/channel_runtime | sort)
```

这是**人工对账**，不是脚本。唯一的工具预算是 CHN-X2 在 web 既有测试文件里加的 5 行断言。

web 侧同理，起点是 `8cbaf3a`：

```bash
git log --grep=CHN- --oneline 8cbaf3a..
git log --oneline 8cbaf3a.. -- src/pages/settings/channels src/api/channel.ts src/hooks/use-channel-request.ts
```

---

## `tests/unit` 先天失败基线

2026-08-05 在 `main @ 75f125d5` 实测：**6 failed / 1486 passed / 761.94s**。

**比对的是失败集合，不是通过数。** 下面这 6 条与 channel 无关，全是 Windows 上通过 bash
子进程渲染配置模板导致的环境性失败。改动后出现**不在这个名单里**的失败才是回归：

```
tests/unit/test_docker_config_template.py::test_default_docker_template_renders_valid_app_config
tests/unit/test_docker_config_template.py::test_docker_template_yaml_escapes_environment_values
tests/unit/test_service_conf_template_render.py::test_rendered_config_passes_app_config_validation
tests/unit/test_service_conf_template_render.py::test_empty_defaults_render_as_strings_not_null
tests/unit/test_service_conf_template_render.py::test_env_values_reach_the_rendered_config
tests/unit/test_service_conf_template_render.py::test_values_are_not_re_interpreted
```

前两条报 `subprocess.CalledProcessError`（`bash D:\project\MultiRAG\docker\...`），
后四条报 `TypeError: 'NoneType' object is not subscriptable`（同一渲染函数返回 None）。
全量跑一次要 **12 分 41 秒**，所以日常用 channel 快速回路，提交前才跑全量。

**2026-08-05 追加（CHN-O1 时发现）**：全量跑偶发第 7 条
`test_channel_provider_spec.py::test_importing_specs_stays_pure`，rc `3221225794`
= `0xC0000142` `STATUS_DLL_INIT_FAILED`——**与上面四条模板测试是同一个 rc**，即
Windows 在长跑后期起不来子进程。单独跑或小切片跑必过。已在该测试里对「这个 rc 且
stdout 为空」`pytest.skip` 并写明「purity unverified」：子进程根本没启动，判过是撒谎、
判挂是冤枉被测代码，**只有「这次没测到」是真话**。CI（Linux）不会触发。

> 这个基线会随环境和其他人的改动变化。**发现数字对不上先重新测一次并更新本节**，
> 不要默认「多出来的失败是我造成的」，也不要默认「就是这 6 条」。

## 待决事项

| ID | 问题 | 需要谁定 |
|---|---|---|
| CHN-Q1 | 第三个 provider 是不是企业微信？它的 `connection_type` 判别式分支会逼出 `visible_when` 与 number 控件，届时 `FormField` 需要扩展 | 产品 |
| CHN-Q2 | 阶段 O 里 O6–O11 的相对优先级（连接自检 / keyring / 审计 / 可观测 / 轮询 / 配额） | 产品 + 运维 |

---

## 变更日志

| 日期 | 变更 | 提交 | 记录人 |
|---|---|---|---|
| 2026-08-05 | 文档集建立。审计结论收敛为 CHN-S/U/P/O/X 五族共 40 个条目；`.gitignore:232` 由裸 `docs` 改为 `docs/*` + 白名单。**验证**：`git check-ignore -v docs/channel-program/README.md` 返回空（exit 1 = 未被忽略）、`docs/feishu-multitenant/PROGRESS.md` 仍命中 `.gitignore:235:docs/*`、`git ls-files docs` 仍只有既有的 `references/http_api_reference.md`、`git status --untracked-files=all docs/` 列出 4 个新文件 | cdc09928 | Claude |
| 2026-08-05 | **记录 `tests/unit` 先天失败基线**（见下方专节）。实测 `PYTHONUTF8=1 uv run --no-sync pytest tests/unit -q`：**6 failed / 1486 passed / 761.94s** | — | Claude |
| 2026-08-05 | **CHN-X1 完成**：读完 `web:src/api/__tests__/channel.test.ts` 全部 **11 条**（不是 10 条）断言，逐条反推出 CONTRACT v1——端点清单、写请求形状、凭据写入语义、状态词表、错误信封。标出 **3 条编码了错误行为的断言**（§6）：`:68` 的测试名 `'…for the Feishu form only'` 把飞书特例固化成期望、`:219`/`:252` 的 `putCalled === false` 把「绑定修改必须塞进 PATCH」固化、`:251` 的 `enabled` 取自可能陈旧 5 分钟的缓存。运行时错误码表**不手抄**——用 grep 实测枚举出 12 个，命令写进 §4.2 供重跑（这条是评审明确指出手抄码表必漏而改的） | — | Claude |
| 2026-08-05 | **PR-0a 完成**（MultiRAG docs-only）：`.gitignore` 修正 + 4 个程序文件 + `AGENTS.md` 核心规则第 5 条（按目录触发的记账义务）+ `CLAUDE.md` 本地/入库分层说明 + `api/channels/README.md` 顶部指针。**验证**：`git status --untracked-files=all docs/` 列出 4 个新文件、`docs/feishu-multitenant/` 仍不可见、`git ls-files docs` 未变 | cdc09928 | Claude |
| 2026-08-05 | **CHN-S1 完成**。抽出共享叶子谓词 `_is_secret_leaf_key`（子串 `secret`/`token`/`password`/`passwd`/`authorization`/`cookie`，外加 `*_key` 后缀与裸 `key`）；`_sanitize_public_config` 改用它，`_contains_sensitive_key` 保持「叶子谓词 ∪ `credential` 整键」——两者**故意只差一个键**，policy 带 credential 块永远是错的，而 config 合法地持有 `credential.app_id`。`*_key` 用后缀而非裸 `key` 子串判定，否则会误杀 `key_id`（标识哪把主密钥加密了该行，非机密）与 `keywords`。**验证**：新增 3 条测试 `test_sanitizer_strips_provider_credential_names_but_keeps_public_ids`（9 个真实 provider 凭据名全被剥离、`app_id`/`corp_id` 保留）、`test_sanitizer_leaves_non_secret_key_names_alone`（`key_id`/`keywords`/`monkey` 不误杀）、`test_policy_forbids_the_credential_block_that_config_may_carry`（两谓词差异被断言锁死）；`pytest tests/unit/test_chat_channel_control.py` **23 passed**（原 20）；`grep '"app_secret", "secret", "token", "api_token"' service.py` 返回空；`ruff format --check` / `ruff check api/channel*` / `lint-imports`（5 kept, 0 broken）全绿 | cdc09928 | Claude |
| 2026-08-05 | **CHN-S2 完成（tolerate 半步）**。三处改动：① `DesiredRuntime.provider` 与 `RuntimeBindingConfig.provider` 由 `Literal["feishu"]` 改为共享的 `ProviderName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]`（用 `Annotated` 而非共享 `Field()` 实例——后者在 Pydantic v2 里是把同一个 `FieldInfo` 挂到两个模型上的坑）；② 逐行降级放在**路由层**而非 service 层，因为 FastAPI 的 `response_model` 会二次校验，只有让路由交出已验过的对象才真正安全；`_short_hash` 在路由文件里本地定义而不是从 `api.channels` import，否则会把 provider SDK 及其进程级事件循环拖进 API 进程；③ `list_desired_runtimes` 的静默过滤补 `CHANNEL_SECRET_MISSING` 警告，刻意不改 runtime 行状态（这是观察不是状态转移）。**验证**：新增 `test_one_unparseable_desired_row_does_not_stall_the_whole_reconcile`——三行输入（feishu / 非法名 / dingtalk）断言响应 200、非法行被丢、**dingtalk 通过**（这同时钉住了放宽本身）、日志含 `CHANNEL_DESIRED_ROW_INVALID` 且**不含原始 binding id**；`pytest` 四个 channel 测试文件 **44 passed**；`ruff format --check .`（1154 files）/ `ruff check .` / `lint-imports`（5 kept, 0 broken）/ `mypy`（61 files, no issues）/ `check_async_sync_db`（新增 0）全绿。**emit 侧仍锁着**：`api/channel_control/schemas.py:10` 的 `ChannelProvider = Literal["feishu"]` 原样不动，那是 CHN-P9 的闸门——按 CHN-ADR-06，第一条非 feishu 行只能在所有 supervisor 都跑上本次改动之后才允许存在 | cdc09928 | Claude |
| 2026-08-05 | **CHN-S3 完成**。`RedisChannelStateStore` 的 `app_id: str` 换成 `scope: tuple[str, ...]`，`_KEY_PREFIX` 升 `v2`；managed 传 `("binding", binding_id)`、demo 传 `("demo", "feishu", app_id)`。`_hash_identifiers` 本来就是 length-prefixed 的，所以 `("a","bc")` 与 `("ab","c")` 不会碰撞。**验证**：新增 4 条测试——`test_two_bindings_on_one_provider_account_do_not_share_a_namespace`（**同一个 Redis、同一个 lease_name**，两个 binding 都能拿到租约；修复前第二个必失败）、`test_one_binding_still_holds_its_own_lease_exclusively`（per-binding 不削弱单 runner 保证）、`test_demo_scope_cannot_collide_with_a_managed_binding`、`test_store_rejects_an_empty_scope`；`pytest test_feishu_state_store.py test_feishu_worker.py` **28 passed**。**一次性代价已写进 `api/channels/README.md` 新增小节**：接住改动的那次 worker 重启，会话重置一次、去重窗口空一次；顺带白送修掉「同 app_id 重建渠道复用 dedupe 命名空间导致静默丢消息」 | cdc09928 | Claude |
| 2026-08-05 | **CHN-S4 完成**。`_ensure_ready` 由 `@staticmethod` 改为 async 实例方法（4 个调用点加 `await`），新增 `_ensure_account_not_already_enabled` + repository 的 `list_enabled_channels`。**查行不查 JSON**：account id 在 `config` 里的位置是 provider 特定的，留在 Python 侧既避开 JSONB 方言差异，也把这份知识集中到 `_account_identity` 一处，等 CHN-P3 接管。**验证**：新增 3 条测试——同租户第二个同账号渠道启用被拒、**两个租户可以各自启用同一个 provider 账号**（这条是防止修复本身变成新的抢占面）、重复启用同一渠道不算自冲突；`pytest test_chat_channel_control.py` **26 passed** | cdc09928 | Claude |
| 2026-08-05 | **CHN-S5 完成（权限 PR）**。归属与授权拆成两个答案：repository 的 `dialog_belongs_to_tenant` 换成 `resolve_dialog_owner`、新增 `resolve_canvas_owner`（返回 `(owner, permission)`）与 `user_can_update_tenant_resources`，`canvas_revision_is_latest_published` 去掉 tenant 过滤。**实测发现两类目标的模型不同**：`UserCanvas` 有 `permission`（me|team），**`Dialog` 没有**——`dialog_service` 的团队口径是 `tenant_id ∈ joined_tenant_ids`，不看 permission。所以规则必须分开：canvas 私有则只有 owner 能绑（角色也不能覆盖），dialog 只看成员身份 + 角色。角色谓词 `can_update_tenant_resources` 直接 import 复用（纯函数），只把查询 async 化，因为 service 层版本收 sync `Session` 而本包受 `check_async_sync_db` 约束必须纯 async。新异常 `ChannelTargetNotAccessible` / `CHANNEL_TARGET_NOT_ACCESSIBLE`。**验证**：新增 5 条测试覆盖「团队共享 + admin → 放行」「团队共享 + normal → 拒绝」「他人私有 + admin → 仍拒绝」「dialog 跨租户按角色」「目标不存在与无权限报不同错误」；既有 `test_canvas_binding_requires_latest_owned_published_revision` 显式注册 canvas 以区分「不存在」与「版本过期」两个新分开的错误；`pytest test_chat_channel_control.py` **31 passed**。**存量不受影响**：`list_desired_runtimes` 与 `resolve_runtime_binding`（私有面）都不调 `_validate_target`，已 grep 确认——检查只在下一次管理面写操作时咬人 | cdc09928 | Claude |
| 2026-08-05 | **CHN-S6 完成**。新增 `scripts/audit_channel_target_authz.py`，只读枚举会被 S5 拒绝的存量 binding，输出 channel/tenant/target/owner/reason 五列，`--strict` 时有发现即 exit 1 供流水线用。**验证**：`mypy scripts/audit_channel_target_authz.py`（scripts 在纳管范围，全注解）Success、`ruff check` 通过、`--help` 实跑可用。踩到 `ModuleNotFoundError: No module named 'api'`——脚本从 `scripts/` 跑时仓库根不在 `sys.path`，照 `init_ai_guard_system.py:14` 的既有先例补 `sys.path.insert` | cdc09928 | Claude |
| 2026-08-05 | **阶段 S 全部完成（S1–S6）**。全门禁实跑：`ruff format --check .`（1155 files）/ `ruff check .` / `lint-imports`（5 kept, 0 broken——新增的 `api.channel_control` → `api.db.services.user_service` 依赖不违约）/ `mypy`（62 files, no issues）/ `check_async_sync_db`（新增 0、双轨 0）；`pytest tests/unit -k "channel or feishu"` **212 passed** | cdc09928 | Claude |
| 2026-08-05 | **全量回归对基线**：`pytest tests/unit -q` 得 **6 failed / 1504 passed**，失败集合与开工基线**逐条相同**（那 6 条 Windows 模板渲染），通过数 1486 → 1504，+18 正好是本阶段新增的测试数。顺带修掉一个既有 gitignore bug：`.dmypy.json  # 说明` 写成了行尾注释，而 **gitignore 不支持行尾注释**，整行被当成模式，所以那条规则从来没生效过，dmypy 守护进程状态文件一直暴露在未跟踪列表里 | cdc09928 | Claude |
| 2026-08-05 | **CHN-U1 完成**。`_respond` 的四个失败分支把 `data=False` 换成 `{"error_code": ...}`，兜底分支也给了自己的码 `CHANNEL_OPERATION_FAILED`——否则最可能到达管理员的那类失败反而是唯一没有可映射文案的。`retcode`/`retmsg` 一个字节没动，前端 `APIError.details` 就是这个对象，零接线。**验证**：新增 `test_failure_envelope_carries_a_machine_readable_error_code`，覆盖三条分支（`ChannelTargetNotAccessible` → ARGUMENT_ERROR + 码、`ChannelAccessDenied` → AUTHENTICATION_ERROR + 码、`RuntimeError("boom…")` → EXCEPTION_ERROR + 兜底码**且响应里不含 "boom"**）；`pytest test_chat_channel_control.py` **32 passed**。跨仓义务已履行：CONTRACT §4.1 改写为已实现形态并列全 5 个码、跨仓联动表 U1 置 ✅ | 86e76adc | Claude |
| 2026-08-05 | **阶段 U 前端六条完成（CHN-U2~U7）**，一个提交，因为同一批文件、彼此无依赖、拆开只会重复跑六遍门禁。要点：① 错误码映射做成纯函数 `channelErrorMessageKey(error, fallbackKey)`，**对任何后端版本都安全**——没有 `error_code` 时回落到今天的文案，未知码也回落而不是把大写标识符甩给管理员；② providers 查询失败不再返回整页 `PageErrorState`，改成内联横幅 + 禁用新建，**渠道列表照常渲染**——原先那个行为在事故中最要命，它把「停用出问题的渠道」这个唯一有用的操作也一起藏了；③ 表单重置守卫用 **ref 而不是 `eslint-disable`**（`react-hooks/exhaustive-deps` 是 error 级，规范明令不许 disable 糊过去），配合 detail 查询关掉 `refetchOnReconnect`/`refetchOnWindowFocus`；④ `setQueryData` 改 `invalidateQueries`；⑤ 提交前 `fetchQuery` 拿新鲜 `binding.enabled`；⑥ 绑定下拉改服务端搜索，`page_size` 100→50 并加搜索框（Radix Select 把可打印键当 type-ahead，所以输入框要 `stopPropagation`）。**验证**：`npm run test:api` **61 passed**（新增 4 条：错误映射在四种降级输入下都回落、每个码各自成键且兜底码在闭集内、状态词表恰好 6 值且三个凭空发明的值不再算健康、`revision_stale` 只在服务端明说时才警告）；`eslint` 0 errors（顺手把 `role="status"` 改成语义化的 `<output>`）；`tsc -b` 通过；`build` + `check:bundle-size` 三档全过（入口 gzip 116/120 KB）；file-size 棘轮通过 | web a2c98c0 | Claude |
| 2026-08-05 | **CHN-P1 完成**。新建 `api/channel_providers/`（`spec.py` / `registry.py` / `feishu.py`），飞书的四个 pydantic 模型从 `channel_control/schemas.py` 迁入并在原处重导出（测试与既有 import 不受影响）。`ProviderSpec` 是 frozen dataclass 而非 Protocol——它是纯数据，Protocol 只会让它更难测。规格里已经放好 `secret_paths` 与 `account_identity_path`，CHN-P3 用它们替换掉服务层那几处硬编码。**删掉死注册表** `api/channels/core/registry.py`：`build_channels` 零调用、`register_channel` 唯一调用点是 `feishu/channel.py:560`，而它注册的 `_build` 正是「从明文 config dict 读凭据」的上游形态——两个都叫注册表、一个活一个死，新 provider 作者根本分不清。`api/channels/provider.py` 的 `_PROVIDER_MODULES` 也删了，改为委托新 registry，**provider 名单从此只有一份**。**验证**：新增 `tests/unit/test_channel_provider_spec.py` 7 条，其中 `test_importing_specs_stays_pure` 用**子进程**断言 `import api.channel_providers` 后 `sys.modules` 里没有 `lark_oapi`/`sqlalchemy`/`fastapi`/`redis`（import-linter 表达不了「不许第三方 SDK」，这半边只能这么守），`test_declared_secret_paths_match_the_models_secret_fields` 反射遍历模型断言 `secret_paths` 与 `SecretStr` 字段**完全一致**——两者是同一事实的两种陈述，不一致就意味着凭据会被写进公开 config 列。**关键验收：`provider_manifests()` 输出与改动前逐字节相同**（`git stash` 前后 dump 对比通过），证明这是纯重构。`lint-imports` **6 kept**（新契约生效）/ `ruff` / `mypy` (62) 全绿；`pytest -k "channel or feishu"` **220 passed** | 80660b37 | Claude |
| 2026-08-05 | **CHN-P2 完成**。`ProviderSpec` 增 `form: ProviderForm`，manifest 随之下发展平的有序 FieldSpec；`config_schema` **一个字节没动**，继续由 pydantic 生成、继续只服务校验与 OpenAPI。这一步真正解开的是那个僵局：`required` 落在 form 层，`FeishuConfigInput` 因此可以名正言顺地保持全字段可选以支持 PATCH merge——前端过去硬编码一份 required 集合，而且编码**错了**（schema 说全都可选），两处各自为真才是正解。**验证**：新增 5 条一致性测试把两份派生物绑住——每个 `form.path` 必须是 `config_model` 真实接受的字段（否则前端渲染出的输入框会被 `extra="forbid"` 拒绝，管理员看到「页面让我填的字段不合法」）、`secret=true` 集合必须与 `secret_paths` **完全相同**、select 必须有 options 且 default 在其中、path 不得重复、manifest 线格形状（含「`config_schema.required` 恒为空」这条反直觉断言）；`pytest test_channel_provider_spec.py` **12 passed**、`-k "channel or feishu"` **225 passed**；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿。跨仓义务已履行：CONTRACT §5 从「尚未存在」改写为完整 payload 示例 + 五条规则 | 819e7ec2 | Claude |
| 2026-08-05 | **CHN-P3 完成**。新增 `api/channel_providers/functions.py`：`split_config` / `merge_config_patch` / `missing_required_fields` 三个通用函数按 spec 声明的路径工作，服务层那三个飞书专属函数退化成一行转发。**5 个硬编码门全部消除**（不止计划里的 4 个）：创建、patch、enable 前置、`resolve_runtime_binding` 的凭据重组、以及账号身份提取。`grep '"app_id"\|"app_secret"' service.py` 现在只剩注释；`grep Feishu service.py` **零命中**。**最危险的一处**：`split_config` 必须用 `model_dump(mode="python")` 而不是 `mode="json"`——pydantic 在 json 模式下把 SecretStr 渲染成字面量 `'**********'`，那串星号会一路写进**会回显**的 config 列，而且能通过所有「检查凭据是否缺失」的判断（它非空）。函数 docstring 里写死了这条。spec 新增 `credential_paths`（凭据的全部路径，含非密钥部分），因为凭据本来就跨两个存储：密钥半边加密、非密钥半边（app_id/corp_id）正当地留在公开 config，worker 拿到的是重组后的完整凭据——声明整个集合才能让重组通用化，而不是去 pattern-match `credential.` 前缀。enable 失败的文案也从「Feishu App ID and App Secret」改成按 spec 的 `form.fields` 拼实际缺失的字段名。**验证**：`pytest test_chat_channel_control.py` **32 passed 且测试文件一行未改**——这是行为保持的证明；新增 `test_secret_paths_are_a_subset_of_the_credential`（密钥若不在凭据集合里，会被加密存下然后永远交不出去）；`-k "channel or feishu"` **226 passed**；`ruff` / `lint-imports`(6 kept) / `mypy`(62) / `check_async_sync_db`(新增 0) 全绿 | 1c80e6ce | Claude |
| 2026-08-05 | **CHN-P4 完成（tolerate 半步），并因此补上了 CHN-ADR-06 的一个漏洞**。`RuntimeCredential` 加 `fields: dict[str, str]` 与 `value(key, legacy=...)` 读取器，飞书 provider 改为经 `value` 取值——两边都能工作。**第一版直接把测试跑红了**，而且红得很有价值：这些模型是生产方与消费方**共用**的，往里加一个带默认值的字段，FastAPI 序列化响应时立刻就把 `"fields": {}` 放到线上，而没升级的 worker 用 `extra="forbid"` 解析会**整包拒绝**、binding 永远起不来。也就是说「只加字段不填值」的 tolerate 步**本身就是破坏性变更**。修法是在路由层 `response_model_exclude={"credential": {"fields"}}` 把它压住，直到 CHN-P8 才放开。**CHN-ADR-06 已补一条**：tolerate 步落地后线格必须逐字节不变，验收方式就是既有的线格断言不加修改地继续通过。**验证**：`test_runtime_config_releases_only_provider_connection_material_to_authenticated_runner`（断言完整响应体相等）**未修改断言内容**通过——它就是抓住这个问题的那条；新增 `test_runtime_credential_tolerates_both_contract_halves` 覆盖三种输入（只有 legacy 对、两者都有时 fields 优先、未知 key 返回空串）；`-k "channel or feishu"` **227 passed**；全门禁绿。顺带把纯度测试的断言消息补上 returncode 与 stderr——它偶发过一次失败而消息是空的，无法诊断 | 1dc940a9 | Claude |
| 2026-08-05 | **修正自己定的规则：契约版本按语义 bump**。原维护协议第 4 条写的是「契约变更 = 改 CONTRACT + bump `channel-api/vN`」，CHN-U1 第一次真用就发现太粗——它是**向后兼容的加法**（老前端只在成功路径读 `data`，失败路径读 `retcode`/`retmsg`，两者未变），bump 它会让 CHN-X2 那条版本断言天天误报，反而训练出「红了就改常量」的坏习惯。改为：加法只记 CONTRACT 变更日志，破坏性变更（删字段/改含义/改必填性）才 bump | 86e76adc | Claude |
| 2026-08-05 | **实测对账机制并修正它**。提交后立刻跑了「复盘节奏」里那两条 git log 对账，机制确实生效——但它把 `041d5df1`、`9a62a81a` 两个**账本建立之前**的 channel 提交也报成了漂移。原命令缺起点边界，会把全部历史都算进去，第一次真用就会淹没在噪声里。已加上 `cdc09928..`（web 侧 `8cbaf3a..`）并补了一行 `comm -13` 的差集写法 | f3fd0a83 后续 | Claude |
| 2026-08-05 | **踩坑记录（会重复踩，写在这里）**：`.claude/settings.json` 的 PostToolUse ruff hook 带 autofix。分两步编辑「先加 import、再加使用点」时，第一步结束的瞬间那个 import 还没有使用者，autofix 判定 F401 未使用**直接删掉**，第二步就报 F821 undefined。规律：**import 必须与使用点在同一次 Edit 里，或者先写使用点再补 import**。本次在三个文件上各踩一次 | — | Claude |
| 2026-08-05 | **修一处账本自身的漂移**：`README.md` §3「下一批」表用的是早期 ID 分配（S1=lease、S3=脱敏），与 `PROGRESS.md` 的一一对应 PR 方案（S1=脱敏、S3=lease）冲突。账本建立当天就漂移，正是维护协议第 1 条要抓的东西——已按 PROGRESS 的分配订正 | cdc09928 | Claude |
| 2026-08-05 | **PR-0b 完成**（web docs-only）：`.gitignore` 白名单 + `docs/channel-frontend-design.md` + 路线图新增 `SEC-4`/`ARCH-6`（ARCH-6 首行是**补记行**，回填 `9ee3c1e`/`6f0e5bd`/`162fb1f` 三个未记账提交）+ `ARCH-2` 状态由「未开始」订正为「部分完成」（`test:api` 已进 CI，10 个契约测试；顺带确认后端 spec 现状，解除该条目的开工前提）+ 攻坚顺序表插入 `1b`/`4b` + `CLAUDE.md`/`AGENTS.md` 双语第 6 项（按目录触发）。**验证**：`git status --short --untracked-files=all docs/` 显示 `?? docs/channel-frontend-design.md`（可入库）、未白名单的 `docs/*` 仍被忽略、`npx prettier --write` 已把表格排版定死避免提交时二次重排 | web 8cbaf3a | Claude |
| 2026-08-05 | **CHN-P5 完成**（web `c294088`）。新建 `src/pages/settings/channels/form-spec.ts`：`readPath`/`writePath`/`resolveFormFields`/`buildFormValues`/`assembleConfig`/`missingRequiredFields`。放在 `form-spec.ts` 而不是组件里，是因为**只有 `src/api/__tests__/*.ts` 能被 CI 门禁碰到**（`package.json` 的 `test:api` 是唯一跑到 channel 代码的脚本），纯函数才够得着。`buildChannelMutationPayload` 从此**一个 provider 字段名都不提**——它过去按名字读 `app_id`/`app_secret`/`domain`/`allowed_open_ids`，意味着第二个 provider 的凭据会被表单收上来、再被这个函数**默默丢掉**，POST 出去的 `credential` 是 `{}`。字段 key 用 `/` 而非 `.`，因为 react-hook-form 把 `.` 当路径分隔符。**验证**：`npm run test:api` 68 passed（新增 `form values start with every secret blank` 等 9 条）；`tsc -b` / `eslint` / `build` / `check:bundle-size` 全绿 | `c294088` | Claude |
| 2026-08-05 | **CHN-P6 完成**。UI 接到 spec，**死表单修掉**：schema 与渲染现在都 key 在 `activeManifest` 上。原来的 bug 是 zod schema 由 `providers[0]` 构建、字段却按所选 provider 渲染，于是选任何非首个 provider 都会在**未挂载的字段**上产生 zod issue——`handleSubmit` 永远进不了成功分支，也永远不报错，保存按钮**静默失效**。`provider-fields.tsx` 重写为按 `field.kind` 渲染（新增 select / switch / string_list），**未知 kind 渲染成 disabled 字段而不是抛错**——这是将来加 OAuth 按钮、条件可见性时老前端能优雅降级的唯一保障（计划 §七明确要求）。拆出 `channel-runtime-banner.tsx` 给 sheet 减重。表单重置守卫用 ref + `isDirty` 实现而不是 `eslint-disable`：AGENTS.md 禁止关掉 error 级规则。**验证**：`npm run test:api` 68 passed；`tsc -b` / `eslint`（channel 目录零问题）/ `lint:file-size` / `build` / `check:bundle-size` 全绿；两个棘轮 json `git diff --exit-code` 干净 | web `a09a09c` | Claude |
| 2026-08-05 | **CHN-P7 完成——跨仓硬依赖的收口**。删掉客户端兜底 manifest 与飞书编译分支（`form-model.ts` 262 → 111 行），`resolveFormFields` 一并删除（它的存在理由就是 tolerate 窗口）。**半态防护写在这个提交内部**：`listProviders()` 丢弃缺 `form` 的 manifest，并把返回类型收窄成新的 `RenderableProviderManifest`（`form` 必填）——「客户端只处理可渲染 manifest」从注释变成类型系统里的事实，谁想把兜底加回来，得先把这个类型放宽回去。对老后端（CHN-P2 未部署）的表现是 providers 为空 → 走 CHN-U2 的横幅「provider 不可用、新建已禁用」，而列表/卡片/启停/删除全部照常。**替换了 CONTRACT §6-A 标记的那条断言**：`nested provider schema is flattened for the Feishu form only`——它的名字自己就承认在钉一个 provider 特例；它真正值钱的语义（服务端发来的密钥不得进入表单）由新的 `the form seeded from a stored channel never carries a secret` 继承，并加了一条 `listProviders drops a manifest this client cannot render` 守住半态。**验证**：`npm run test:api` 68 passed；`tsc -b` / `eslint` / `lint:file-size` / `build` / `check:bundle-size` 全绿；两个棘轮 json 无 diff | web `a09a09c` | Claude |
| 2026-08-05 | **CHN-O1 完成**。`revision_stale` 从一个「提示」升级成**故障态**：绑定启用、runner 活着、心跳新鲜、代次也对得上，但绑定的 Canvas 版本已经不是最新发布——执行层对**每一条消息**都用 `TargetRevisionUnavailableError` 拒绝，而管理页显示一切正常。现在 `_serialize_runtime` 在这种情况下报 `state="error"` + `last_error_code`，**新鲜心跳不再构成健康证据**（它恰恰是让故障隐形的东西）。两个刻意的边界：① runner 字段保留（进程真的活着，运维需要知道是哪个）；② **binding 未启用时不覆盖**，`stopped` 才是真话，否则每个被人为暂停的渠道都会挂红牌，而 staleness 本来就在 `binding.revision_stale` 上看得见。同时修掉「专用 runtime 路由比列表**更**不准」——`get_runtime` 过去根本不算这个标志，也就是说运维打开的那个诊断面板，是全站唯一从不提原因的地方。**偏离计划一处并说明理由**：计划写的是新造 `TARGET_REVISION_STALE`，实际改用执行层**已有的** `TARGET_REVISION_UNAVAILABLE`——同一个事实两个名字，会让排查一条静默渠道要 grep 两个串。没有 import 而是复制常量：`api.channel_execution` 反向依赖 `api.channel_control`，为一个常量制造 import 环不值当，改用 `test_stale_revision_error_code_matches_the_executor` 把两者绑死。**跨仓义务已履行**：web 侧 sheet 的 runtime 横幅补上原因行（卡片早就有，sheet 没有——于是运维打开的那个面板只说 error 不说为什么）；这是**加法**，老后端 `last_error_code` 为 null 时什么都不渲染，故按语义 bump 规则不 bump 版本，只记 CONTRACT 变更日志。**验证**：新增 3 条（健康 runner 上的 stale 变 error 且列表与专用路由**必须一致**、停用的 stale 报 stopped 不报 error、错误码与执行层一致）；`test_chat_channel_control.py` **35 passed**（32 → 35）；原来那条 `assert stale["runtime"] == fresh["runtime"]` 的注释同批订正——它现在只对未启用绑定成立，留着会变成误导性的「绿色断言」；`ruff` / `lint-imports`(6 kept) / `mypy`(62) / `check_async_sync_db` 全绿；web 侧 `test:api` 68 passed + `tsc` / `eslint` / `build` / `check:bundle-size` 全绿、两个棘轮无 diff | 本次提交 + web `a752f3e` | Claude |
| 2026-08-05 | **CHN-O2 完成（tolerate 半步）**。`RuntimeBindingConfig` 加 `policy: dict[str, Any]` 与 `private_chat_only` 属性，`FeishuBindingBridge` 收 `private_chat_only` 参数，worker 从 `runtime.private_chat_only` 取值。**修的是一个从头到尾都是装饰品的开关**：`policy.private_chat_only` 被表单收集、被服务层校验（还专门查过它不含凭据）、被存进 DB——然后停在那里，worker 里那行 `chat_type != "p2p"` 是无条件的，所以关掉它什么也不会发生。**类型选择**：`policy` 用 `dict[str, Any]` 而不是带类型的 `extra="forbid"` 模型——policy 列按设计就是自由 JSON，只校验形状与「不含凭据」，用闭集模型会让 worker 拒绝一个控制面明明已经存下的 policy，那是最坏的发现地点；未知键必须能搭车通过，否则将来任何一个新开关都会变成全机队故障而不是一个被忽略的字段。**默认值方向是安全方向**：policy 缺失或值不是 bool 一律按 `True`（只服务单聊）处理——猜错的代价是机器人开始在它所在的每个群里回话。同批把「群聊放开」与「非 user 发送者放开」拆成两个独立判断，放宽会话范围不能顺带放宽发送者类型，否则两个机器人能互相刷。**tolerate 验收（CHN-ADR-06 修订条款）**：路由 `response_model_exclude` 从 `{"credential": {"fields"}}` 扩成 `{"credential": {"fields"}, "policy": ...}`，`test_runtime_config_releases_only_provider_connection_material_to_authenticated_runner`（断言完整响应体相等）**一行未改照常通过**——线格逐字节不变，这就是它是 tolerate 步而不是破坏性变更的证明。**验证**：新增 3 条（policy 三态解析 + 未知键前向兼容、畸形 policy 不放宽、群聊开关真的生效且不顺带放宽发送者）；`-k "channel or feishu"` **233 passed**（230 → 233）；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | 本次提交 | Claude |
| 2026-08-05 | **CHN-O5 完成**。`docker/docker-compose.yml` 新增 `multirag-channel-supervisor`（`channel` profile，opt-in）。这条修的是整个程序里影响面最大的一件事：**默认部署下 channel 功能 100% 不工作，而且不工作的方式没有任何提示**——管理页能建渠道、能存凭据、能点启用，运行时永远停在 `waiting`，偏偏这个 `waiting` 与「worker 正在启动」是同一个值，UI 分不出来。**主密钥隔离做成配置而不是约定**：supervisor 的 `environment` 里把 `CHANNEL_SECRET_ENCRYPTION_KEY` 与 `MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY` **显式置空**（compose 里 `environment` 优先级高于 `env_file`，所以运维把密钥写进 `.env` 也盖不进来），并且**不挂 `../configs`**——那是 API 渲染配置的落点，共享它等于用文件系统把密钥递过去。worker 子进程要的 Redis 配置改为显式覆盖传入，因为镜像内置 configs 写的是 `127.0.0.1:6379`，在容器里没有意义。**刻意不写 `depends_on`**：它要依赖的是 cpu 或 gpu（取决于对方 profile），跨 profile 声明依赖会在只启一个 profile 时报 undefined service；API 未就绪只是跳过一轮 reconcile，配置缺失则 exit 1 由 docker 重启退避接住。API 侧（cpu 与 gpu 两个 service）同批补上这两个变量，否则内部私有路由是关的、supervisor 起来了也没人可谈。**排序理由兑现**：刻意排在 CHN-P4 / CHN-O2 之后，所以第一次跑起来的 supervisor 就已经在最新契约上，不需要立刻做一次滚动升级。**验证**：`docker compose --profile channel --profile cpu config` 通过；实测注入 `CHANNEL_SECRET_ENCRYPTION_KEY=leaked-...` 后渲染结果里 API 拿到该值、supervisor 的对应变量为 `''` 且**全部环境变量中该值零命中**；新增 `tests/unit/test_docker_channel_supervisor_service.py` **4 条**把这些钉死（直接解析 yaml，不依赖机器上有 docker 二进制）；`ruff` / `mypy`(62) 全绿。**未做运行时验证**：本机没有起过这套 compose，上线前请按 `docker/README.md`「Channel supervisor」一节的排查清单实际跑一次 | 本次提交 | Claude |
| 2026-08-05 | **CHN-X2 完成**，并顺手修掉一处**服务端注释自相矛盾**：`ProviderForm.version` 上一句写「只在客户端必须反应时 bump」，下一句写「未知的更高版本仍应渲染」——这两条不能同时成立，而且第二条正好否定了这个字段存在的理由。定死的规则是：**加字段/加 kind/加 option 不 bump**（未知 kind 已渲染成 disabled、未知键本来就忽略，加法零协调成本）；**只有老客户端会渲染错时才 bump**，而 bump 的含义是「不认识就拒绝渲染」。落地成前端两个常量：`CHANNEL_API_VERSION`（计划里那 5 行断言，全部工具预算）与 `SUPPORTED_FORM_VERSION`（`listProviders` 真的按它过滤）。**为什么加第二个常量**：只断言一个自己定义、自己断言的字符串是同义反复，唯一作用是「bump 时必须改测试」这个绊线；把它接到线上已有的 `form.version` 上，才让它从绊线变成活的降级开关，成本相同。过高版本走与「缺 `form`」完全相同的降级路径。**验证**：`test:api` **69 passed**（68 → 69，两条断言 + 过滤测试扩成三个 manifest 的 fixture）；`tsc` / `eslint`(channel 目录 0 error) / `build` / `check:bundle-size` 全绿、两个棘轮无 diff；后端 `ruff` / `mypy` 全绿 | 本次提交 + web `9873e25` | Claude |
| 2026-08-06 | **收尾全量回归 + 对账**。`pytest tests/unit -q` 得 **6 failed / 1528 passed / 1 skipped**：失败集合与开工基线**逐条相同**（那 6 条 Windows 模板渲染），通过数 1486 → 1528，那 1 条 skipped 是 CHN-O1 里给纯度测试加的「子进程起不来就跳过」分支按设计生效。跑了复盘节奏里的两条 git log 对账，**两个仓都零漂移**（MR：11 个带 CHN- 的提交 ⊇ 8 个碰 channel 目录的提交；web：零差集）。全门禁绿：`ruff format --check`(1161) / `ruff check` / `lint-imports`(6 kept) / `mypy`(62) / `check_async_sync_db`(新增 0)；web `test:api` 69 passed + `tsc` / `eslint` / `lint:file-size` / `build` / `check:bundle-size` 全绿、两个棘轮 json 无 diff。**同批修 README §3 的严重漂移**：它还写着「当前阶段：阶段 U · 未开始」，而 U/P1–P7/O1/O2/O5/X1/X2 全部已完成——重写成以**部署闸门**为主语，因为零上下文接手时最需要知道的不是「还剩几条」，而是「剩下的都不是缺代码，是不许现在合，以及解除闸门要做什么运维动作」 | 本次提交 | Claude |
| 2026-08-06 | **CHN-P8 + CHN-O3 完成（两个 emit 半步），闸门是查出来的不是假设的**。上一轮我说「剩下的都卡在部署上」，然后**去查了**，结论要修正：`docker ps -a` 里没有 supervisor 容器，但 Redis db1 里有 `multirag:channel:v2:...:leader:...`——**v2 是 CHN-S3 才引入的命名空间**，说明有 worker 在跑且是近期构建；`t_ai_channel_runtime_status` 有一行 15 秒前的 `connected` 心跳，runner `vm-duxiaolong-17364` 就在本机。`Get-CimInstance Win32_Process` 给出决定性证据：**supervisor PID 59548 起于 8/5 09:44**（早于 `cdc09928` 15:31，旧代码），**worker PID 20304 起于 8/6 08:37**（晚于 `1dc940a9` 8/5 16:52 与 `00e4c2c0` 8/5 17:56）。worker 是 supervisor 每次 spawn 的**全新解释器**，按盘上代码加载——Redis 是 v2 而 supervisor 比 S3 还老，正好互证。所以 `RuntimeBindingConfig` 的消费方（worker）**已经带着两个 tolerate 半步在跑**，闸门满足；而 `DesiredRuntimeList` 的消费方（supervisor）**没有**，CHN-P10 的闸门仍然真实存在。**落地**：路由删掉整个 `response_model_exclude`，`credential.fields` 填入完整凭据、`policy` 原样下发；`ResolvedRuntimeBindingSpec` 加 `policy` 字段。legacy 的 `app_id`/`app_secret` 对**继续发**——CHN-P4 之前起的 runner 把它标成 required，删了是解析失败而不是降级，那是 CHN-P11 的事。**顺手修掉一个哑掉的 fake**：`FakeSecretStore.decrypt` 恒返回 `{}`，导致任何走到 `resolve_runtime_binding` 的调用都撞「credential incomplete」——也就是说**从来没有测试跑到过给 runner 重组凭据那段代码**。改成真往返后才写得出下面两条。**验证**：线格断言按 emit 语义改写（`fields` 与 `policy` 上线，那批「不得出现 tenant_id/target_id/...」的安全断言一条没动，`policy` 移出禁列并写明理由）；新增 2 条（重组后的凭据含加密半边与公开半边、policy 端到端到达且未知键原样透传、含凭据的 policy 仍被拒）；`-k "channel or feishu"` **238 passed / 1 skipped**；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | 本次提交 | Claude |
| 2026-08-06 | **CHN-P9 完成**。`ChannelProvider` 从 `Literal["feishu"]` 改为读注册表的 `Annotated[str, AfterValidator]`；`ChannelCreateRequest.config` / `ChannelUpdateRequest.config` 改 `dict[str, Any]`，在 service 层按 provider 派发到 `spec.config_model` / `config_patch_model` 二次校验。**为什么不能在请求模型里校验 PATCH**：PATCH 体里根本没有 provider 名，只有库里那行知道自己是什么 provider——所以线上类型只能是开放对象，判别必须发生在 service。**闸门分析（与 CHN-P10 分开的理由）**：`DesiredRuntimeList.provider` 的值来自 `channel.channel` 列，而这一列的值受创建时的注册表校验约束；注册表里只有 feishu，所以**本条不会让任何新值上线**，实测 `provider_manifests()` 仍是 `['feishu']`。旧 supervisor（PID 59548，起于 8/5 09:44，早于 CHN-S4 的 `provider` 放宽）因此不受影响。真正的闸门落在 **CHN-P10**——注册钉钉的那一刻起，`DesiredRuntimeList` 里才可能出现 `provider: "dingtalk"`，旧 supervisor 会 ValidationError 并**跳过整轮 tick**，把健康的飞书 binding 一起拖停。**一处安全细节**：把 pydantic 的报错转成 `INVALID_CHANNEL_CONFIGURATION` 时**只取 `loc` 与 `msg`，绝不取 `input`**——`ValidationError.errors()` 带 `input` 键，照抄会把被拒的 `app_secret` 原样写进 API 响应体和承载它的日志。已有专门测试断言报错里既没有密钥值也没有被拒的字段值。**一处已知代价**：OpenAPI 里 `config` 从 `FeishuConfigInput` 退化成裸 `object`，per-provider 的 schema 只能从 manifest 的 `config_schema` 取——这正是 CHN-ADR-03 给 `config_schema` 保留的用途，但对纯 OpenAPI 消费方是实实在在的损失，记在这里。**验收**：`-k "channel or feishu"` **238 passed**且**测试文件在重构那一步一行未改**（行为保持的证明），之后才追加 3 条新断言（未知 provider 被注册表拒、报错不回显值、PATCH 按存储行的 provider 校验）；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | 本次提交 | Claude |
| 2026-08-06 | **CHN-O4 完成**。三件事：① `FEISHU_WS_STOPPED` → `CHANNEL_TRANSPORT_STOPPED`——发出它的 `_monitor_channel` 监视的是 `Channel` 协议，本来就与传输无关，旧名字在第一个非飞书 provider 上就是错的；`last_error_code` 是自由字符串（无 Literal），所以这是加法不是破坏性变更。② `FeishuBindingBridge` → `BindingBridge`，`allowed_open_ids` → `allowed_sender_ids`。**这个类的名字一直在撒谎**：它只跟 `Channel` 协议对话、按 `message.channel` 做会话键、从不 import 任何 provider——名字是第二个 provider 唯一需要绕开的东西。测试文件同步 `git mv` 成 `test_binding_bridge.py`。③ **capabilities 闸门**：`private_chat_only = policy OR not spec.capabilities.group_chat`，**两个独立门取窄**——管理员只能放宽到传输真正能承载的范围，否则在一个不支持群聊的 provider 上关掉开关，机器人会去读它根本无法回复的群消息。capabilities 从 `provider_spec(name)` 读而不是往 `WorkerProvider` 协议上加：前者是控制面描述符、后者是传输描述符，既有分层就是这么分的。**保留不动**：`FeishuAgentBridge` 与 `FEISHU_CHANNEL_DISABLED` 在 demo 路径上（走 env 配置、无 binding），叫这个名字是对的。**验证**：新增 1 条（不支持群聊的 provider 无视 policy 仍然拒群消息，并断言飞书今天确实 `group_chat=False`）；`-k "channel or feishu or binding"` **243 passed**；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | 本次提交 | Claude |
| 2026-08-06 | **CHN-P10 spec 半边完成，transport 半边未做（🔵 进行中）**。新增 `api/channel_providers/dingtalk.py`：`client_id` + `client_secret`（钉钉控制台现在的叫法，老版本叫 AppKey/AppSecret）、`robot_code`、`allowed_user_ids`。**四个字段用的全是前端已有的 kind**，这正是整个 FieldSpec 契约要证明的事。**没做 transport 并且不注册**，理由是链式的：注册 → `/providers` 出现钉钉 → 管理员能建能存 → 一点启用，`DesiredRuntimeList` 里出现 `provider: "dingtalk"`，**旧 supervisor（PID 59548，起于 8/5 09:44，早于 CHN-S4 的 `provider` 放宽）会 ValidationError 并跳过整轮 tick，把健康的飞书 binding 一起拖停**；就算 supervisor 是新的，worker 也会在 `transport_module("dingtalk")` 上炸——那个模块不存在。收消息需要 `dingtalk-stream`（未安装；已有的 `alibabacloud-dingtalk` 是 HTTP API SDK，只能发不能收），加依赖是用户的决定，另有 webhook 方案但那要开一个公网入站路由，安全面完全不同。**顺手改进了测试设计**：那批一致性测试原先只 parametrize `provider_specs()`（**已注册**集合），意味着一个写好但还没接线的 spec 零覆盖，而第一个碰它的动作就是注册本身——恰恰是坏 spec 代价最大的时刻。改为扫描包内所有声明了 `PROVIDER_SPEC` 的模块，并加 `test_every_registered_provider_has_a_declared_spec` 断言注册集 ⊆ 声明集。**新增 P10 的验收断言**：`test_a_provider_form_needs_no_widget_the_client_lacks` 把 `web:ChannelFieldKind` 镜像成一个集合，断言每个 spec 的 kind 都在里面——出现新 kind 就意味着需要先发前端，而那正是这套契约要消灭的东西，所以往这个集合里加一项必须是一次自觉的跨仓决定。**验证**：钉钉 spec 通过全部 10 条既有一致性检查（路径存在于模型、`secret_paths` 与 `SecretStr` 反射一致、secret ⊆ credential、path 不重复……）；`test_channel_provider_spec.py` **23 passed**（13 → 23）；`-k "channel or feishu or binding"` **253 passed**；`provider_manifests()` 实测仍是 `['feishu']`，确认未注册；`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | 本次提交 | Claude |
| 2026-08-06 | **CHN-X3 静态验收完成（🔵：活体联调等 transport）**。跨仓验收拆成两条互相咬合的测试，各在自己仓的 CI 里跑：web 侧 `a provider this build has never heard of renders and submits` 拿**后端真实产出的钉钉 manifest**（逐字节照抄，含全部 null）作 fixture，断言 `getChannelFormDefaults` 给出正确的 config/secrets 分桶、`missingRequiredFields` 认出三个必填、`assembleConfig` 组装出嵌套 payload——**整个测试文件不提任何钉钉字段名**，全部由服务端字段表推导；MR 侧 `test_the_payload_the_client_assembles_is_the_one_the_provider_accepts` 断言同一个字面量能被 `DingTalkConfigInput` 接受，并被 `split_config` 正确切成公开半边与加密半边。**两个仓 CI 独立、无共享 fixture，一对断言同一字面量的测试就是唯一的绑定机制**——任一侧漂移，另一侧红。顺带把 `RENDERABLE_KINDS` 从 `provider-fields.tsx` 的内联条件提到 `form-spec.ts`：渲染行为一字未改，但这个列表原先待在唯一测试够不到的地方，而它恰恰决定「没见过的 provider 拿到的是可用表单还是一排 disabled 占位」。**验证**：web `test:api` 69 → **76 passed**、`tsc` / `eslint` / `lint:file-size` / `build` / `check:bundle-size` 全绿、两个棘轮无 diff；MR `test_channel_provider_spec.py` **23 passed / 1 skipped** | 本次提交 + web `d4adb18` | Claude |
| 2026-08-06 | **事故与订正：一次 `git add -A src` 把用户正在改的 auth 工作卷进了 channel 提交**（`src/api/auth.ts`、`stores/auth.ts`、`client.ts`、`team.ts`、`types/*`、`system-setting.tsx`、新文件 `__tests__/auth.test.ts`，共 8 个文件 / 约 600 行）。已 `git reset --soft` 拆开重提（`e738190` → `d4adb18`，只含 3 个 channel 文件），auth 那批还原成未提交的工作区改动。**并核实 lint-staged 的 `prettier --write` 有没有改到用户的内容**：用它自己留下的备份 commit `9b2ac0b` 逐文件 `diff --no-index`，**8 个文件全部零 delta**，未被改动。同批复查本会话 web 侧另外三个提交（`a09a09c` / `a752f3e` / `9873e25`），**均只含 channel 文件，未被污染**。教训写在这里：这个仓的工作区随时可能有用户并行的改动，**提交一律显式列路径，不用 `git add -A`**。文件体积门禁那句 `src/types/api.ts（1499 → 1495 行）` 从会话一开始就在报，那正是这批改动的信号，我一直当成了无关噪声 | 本次提交 | Claude |
| 2026-08-06 | **收尾**：全量回归 **6 failed / 1545 passed / 1 skipped**，失败集合与开工基线逐条相同（那 6 条 Windows 模板渲染），通过数 1486 → 1545。全门禁绿（`ruff format --check` 1162 / `ruff check` / `lint-imports` 6 kept / `mypy` 62 / `check_async_sync_db` 新增 0）；web `test:api` 76 passed + 全部前端门禁绿、两个棘轮无 diff。README §3 重写：从「全部卡在部署上」（那个判断在**实测之后被推翻了三条**）改成「24 个 PR 落地 23 个，只剩两件事」，并把**怎么查闸门**（`docker ps` / Redis 键 / runtime_status 心跳 / `Get-CimInstance` 进程创建时间 vs 提交时间）写进去——下一个接手的人应该先查再假设 | 本次提交 | Claude |
| 2026-08-06 | **supervisor 已重启到当前代码，CHN-P8 / CHN-O3 的 emit 半步在生产里验证通过**。旧树（uv.exe 14668 → 59548 → 47248 → worker 20304/17364）整棵杀掉后重起，新 runner `vm-duxiaolong-14064`，`.err.log` 里 `ws_connected` + `worker_started result=ok`，`t_ai_channel_runtime_status` 心跳新鲜、`last_error_code` 为空，Redis leader 键回来，**`RUNTIME_CONFIG_INVALID` 零命中**——也就是说这个 worker 用当前代码拉到了含 `credential.fields` 与 `policy` 的 `runtime-config` 并正常解析、正常连接。两个 emit 半步至此不只是测试通过，是**活体通过**。副作用如预期：会话重置一次、dedupe 窗口空一次。**CHN-P10 的注册闸门随之打开**：supervisor 现在带着 CHN-S4 的 `provider: str` 放宽，第二个 provider 不会再让它跳过整轮 tick | 本次提交 | Claude |
| 2026-08-06 | **踩坑（会重复踩，已写进 README §3）**：第一次重启我直接 `Start-Process uv run python -m api.channels.supervisor`，立刻 `error_code=CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED` 退出，**飞书 bot 停了约 5 分钟**。原因：supervisor 的 `RUNTIME_API_BASE_URL` 与 `INTERNAL_API_TOKEN` **不在 `configs/*.yaml` 里**——两个配置文件都没有 `channels:` 段，值来自 `%LOCALAPPDATA%\MultiRAG\secrets\supervisor.env`，由 `scripts/run_channel_supervisor.example.ps1` 载入（那个脚本还会主动拒绝带主加密密钥启动）。**排查顺序的教训**：我先翻了 shell 历史（零命中）才想起 `scripts/*.ps1`——下次先看启动脚本。另外 logging 默认写 stderr，`channel-supervisor.log` 是空的，**要看 `.err.log`**，我差点据此判定进程没输出 | 本次提交 | Claude |
| 2026-08-06 | **CHN-P10 完成（transport + 注册），依赖问题不存在**。按用户要求先读了上游 ragflow 的实现（`api/channels/dingtalk/channel.py`，423 行）：它**用 aiohttp 手写 DingTalk Stream 协议**——`POST /v1.0/gateway/connections/open` 拿 endpoint + ticket → websocket → 每条 callback 回 ack → 回复发到消息自带的 `sessionWebhook`。**没有引入 `dingtalk-stream`**，而 `aiohttp` 我们早就有。所以我之前提给用户的「加依赖 vs webhook」二选一是个伪命题，第三条路才是对的。目录结构照上游：`api/channels/dingtalk/{__init__,channel,provider}.py`。**没有照抄的三处**：① 上游在 channel 里维护 `_processed_message_ids` / `_inflight_message_ids` 两个进程内去重字典——我们有 Redis `ChannelStateStore` 按 binding 去重，再加一层进程内的会是第二套更弱的机制，去重留在 bridge；② 上游每发一条消息新建一个 `aiohttp.ClientSession`，改成复用；③ 上游 websocket 连接试三种模式（query/header/bare）——保留前两种（endpoint 实测可能自带 query，这是唯一没有真实租户就验不了的地方），去掉 bare，并按我们的结构化日志约定重写全部日志、标识符一律哈希。**安全细节**：`connections/open` 的请求体带 client_secret 而钉钉失败时会回显请求上下文，所以**响应体永不进日志**，只记 status。**一处刻意的行为**：拿不到 chat_id/sender_id/正文时**仍然先 ack 再丢弃**——不 ack 的 callback 会被无限重投，为了一条我们本来就不打算回的消息制造死循环，比丢一次回复糟得多。**新增配置段** `channels.dingtalk`（只有调优项，没有连接凭据：钉钉只走 managed 模式，不像飞书还留了一条读环境配置的 demo 路径）。**两条钉死「只有飞书」的测试按预期红了**，改成从注册表推导而不是换个名字继续钉——`supported_provider_names()` 现在断言「已排序、去重、且与 `provider_names()` 相等」，providers 路由断言「返回顺序 == 注册表顺序」。**验证**：新增 `test_dingtalk_channel.py` **11 条**（callback 归一化、群聊标注、三种不可回答载荷仍被 ack、回复目标来自消息而非 chat_id、畸形信封不进 handler、ticket 拼接保住 endpoint 自带的 query、worker 描述符**只读 generic 凭据**且拿 legacy 对时 fail closed、畸形 allowlist fail closed、租约续租早于过期）；`-k "channel or feishu or binding or dingtalk"` **254 passed**；全量 **6 failed / 1557 passed**（失败集合＝基线）；`ruff` / `lint-imports`(6 kept) / `mypy`(62) / `check_async_sync_db` 全绿；`provider_manifests()` 实测 `['dingtalk', 'feishu']` | 本次提交 | Claude |
| 2026-08-06 | **CHN-P10 验收：`git diff --stat` 里零个 `web/` 路径**——本条从 spec 到 transport 到注册，web 仓一次提交都没有。前端能渲染钉钉表单这件事由 CHN-X3 那对咬合测试固定（web `d4adb18` 用后端真实 manifest 作 fixture，MR 侧断言同一字面量被 `DingTalkConfigInput` 接受并正确切分）。**至此整个程序的核心命题成立**：第二个 provider 落地，前端零改动。| 本次提交 | Claude |
| 2026-08-06 | **发现一条与 tolerate/emit 无关的独立闸门**：`supervisor.py:35` 的 `_SUPPORTED_PROVIDERS = frozenset(supported_provider_names())` 是**模块级常量、import 时冻结**。也就是说**注册任何新 provider 都需要重启一次 supervisor**，与契约版本无关。好消息是它 fail-safe 而不是 fail-stop：不认识的 provider 走 `:128` 的 `provider_unsupported` 分支**逐条跳过**，健康的飞书 binding 照常 reconcile（这正是 CHN-S4 逐行降级的形状）。当前跑着的 supervisor 起于 10:30、早于本次注册，所以**它现在会跳过钉钉 binding**——建钉钉渠道前需要再重启一次。没有自作主张再重启：用户只授权了一次，而且目前一个钉钉渠道都还没有 | 本次提交 | Claude |
| 2026-08-06 | **CHN-P13 完成：provider 可发现性 + 钉钉 i18n**。原交互是「一个新建按钮 → 抽屉里一个下拉」，**页面上没有任何地方告诉用户能接入什么**，答案只有开始创建之后才看得到。改成两段：「已接入渠道（N）」+「可接入渠道」卡片画廊（logo / 名称 / 一句话说明 / 接入按钮 / 已接入数量徽章），点卡片直接带着选中的 provider 打开抽屉。**参照了上游 ragflow 的 UX 形状，但没抄它的数据来源**：上游的 `channelTemplates` 是客户端硬编码的 `ChatChannelKey` 枚举过滤出 7 个——正是这个程序花了 24 个 PR 消灭的模式（provider 的第二个声明点，且是会被忘掉的那个）。我们的画廊完全由 manifest 驱动，服务端注册一个 provider 就自动出现，**只有 logo 是客户端资产**（不认识的 provider 回落到中性图标，与未知 `kind` 渲染成 disabled 同一原则）。**可达性也没抄**：上游把 `onClick` 挂在 `<article>` 上，Tab 到不了、回车不响应；我们用 `<button>`。**服务端加了 `description` + `description_i18n_key`**——描述必须服务端拥有，否则加 provider 又要改前端，CHN-P10 刚证明的那条不变量就破了；前端按 `t(key, {defaultValue: manifest.description})` 消费，本地翻译优先、服务端英文兜底。同批做掉钉钉四个字段的中英文案；locale 里 `providers.<name>` 从字符串改成 `{name, description}`（原来只有 `feishu: '飞书'`，与新的描述块撞键，tsc 直接报 TS1117），`channel-card.tsx` 与抽屉下拉同步改成读 `.name`。**从 ragflow 复制了两个 logo**（`dingtalk.svg` / `feishu.svg` → `web:src/assets/svg/chat-channel/`），只复制已注册的两个而不是全部 21 个——用不上的资产就是死资产，需要时再复制。**验证**：`tsc` / `eslint`（channel 目录 0 error）/ `test:api` **76 passed** / `lint:file-size` / `build` / `check:bundle-size`（入口 gzip 116KB，未增长）全绿、两个棘轮无 diff；后端 `-k "channel or feishu or dingtalk or binding"` **264 passed**、`ruff` / `lint-imports`(6 kept) / `mypy`(62) 全绿 | `3a82e5f6` + web | Claude |
| 2026-08-06 | **补齐待办任务的执行简报 + 交接说明**。用户要把剩余任务派给「没有任何上下文的我」，而原来的任务表只有一句话索引，不够开工。PROGRESS 新增「待办任务简报」一节：CHN-P11 / O6–O11 每条给出**问题（带已核实的证据）/ 为什么值得做 / 闸门 / 验收标准 / 从哪读起**，并写明每条**不该做什么**（例如 CHN-O6 不要给「还没保存」的场景做自检——那需要新开一个明文凭据入口，多一处泄漏面不抵收益；CHN-O7 先只做读侧，重加密单独排一条）。**两处证据是现查的不是回忆的**：`secret_store.py` 的 `decrypt` 第一行判断 `encrypted.key_id != self._cipher.key_id` 就报 `SecretStoreUnavailable`，确认了**只有一把活跃密钥、轮换即全量凭据永久不可解**——所以 CHN-O7 是个「泄漏了也不敢换」的运维陷阱，不是缺功能；`web:use-channel-request.ts` 的 `refetchInterval` 是固定 15 秒，与渠道状态和页面可见性无关，那是 CHN-O10 的全部主题。README 新增 §3.5「怎么把一条任务派给没有任何上下文的我」：**说 ID，别说需求**——复述背景反而危险，因为复述的是作者记忆里的仓库状态，而文档跟的是它现在的状态。附反面例子与四条补充：一次只派一条（任务间有闸门依赖）、要先复核锚点就明说、涉及重启线上进程的先问、不确定派哪条就让我读 README 给建议 | 本次提交 | Claude |
