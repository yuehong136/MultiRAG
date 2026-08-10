# 决策记录（CHN-ADR）

> 记录**为什么**这么选，尤其是与直觉相反的那几条。
> 新决策追加在末尾，不要修改历史条目——要改就写一条新的，并在**两侧**都标注：
> 新条目写 `supersedes`，旧条目的状态改成 `🔁 已被取代（见 CHN-ADR-NN）`。
> （`docs/feishu-multitenant/DECISIONS.md` 只做了单向标注，被取代的条目仍写着 ✅ 采纳，
> 读者只能靠文件头的说明才知道它已经死了——这里不重复那个坑。）

## 索引

| ID | 标题 | 状态 |
|---|---|---|
| [CHN-ADR-01](#chn-adr-01--授权加在-binding-target-上不在-channel-路由上加角色校验) | 授权加在 binding target 上，不在 channel 路由上加角色校验 | ✅ 采纳 |
| [CHN-ADR-02](#chn-adr-02--运行时状态走自适应轮询否决-sse--websocket) | 运行时状态走自适应轮询，否决 SSE / WebSocket | ✅ 采纳 |
| [CHN-ADR-03](#chn-adr-03--服务端展平-fieldspec前端不编译-json-schema) | 服务端展平 FieldSpec，前端不编译 JSON Schema | ✅ 采纳 |
| [CHN-ADR-04](#chn-adr-04--leader-lease-用-binding-维度而不是租户维度) | leader lease 用 binding 维度而不是租户维度 | ✅ 采纳 |
| [CHN-ADR-05](#chn-adr-05--文档分层入库讲我们的代码本地讲别人的代码) | 文档分层：入库讲我们的代码，本地讲别人的代码 | ✅ 采纳 |
| [CHN-ADR-06](#chn-adr-06--私有-runtime-契约的每次变更都拆成-tolerate--emit-两个-pr) | 私有 runtime 契约的每次变更都拆成 tolerate + emit 两个 PR | ✅ 采纳 |
| [CHN-ADR-07](#chn-adr-07--provider-与执行目标正交历史事务由目标驱动拥有) | Provider 与执行目标正交，历史事务由目标驱动拥有 | ✅ 采纳 |
| [CHN-ADR-08](#chn-adr-08--canvas-与-channel-演进以上游同步为主只在适配层吸收现代执行不变量) | Canvas 与 Channel 演进以上游同步为主，只在适配层吸收现代执行不变量 | ✅ 采纳 |

---

## CHN-ADR-01 · 授权加在 binding target 上，不在 channel 路由上加角色校验

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：审计发现 `api/apps/restful_apis/chat_channel_api.py` 的九条路由全部把 `Principal.id`
直接当 tenant_id 用，零 `UserTenantService.can_manage_*` 校验，而同仓 `tenant_api.py:71,82`
就在旁边正确地做了这件事。直觉结论是「九条路由补角色校验」。

**关键新事实（推翻了直觉结论）**：`api/db/services/user_service.py:289-290` 是

```python
if user_id == tenant_id:
    return UserTenantRole.OWNER
```

而 channel 路由传的正是 `Principal.id`。于是 `get_role_in_tenant(user.id, user.id)` 恒返回
`OWNER`，任何 `can_manage_*` 检查恒为 True。**在九条路由上加校验是纯表演，关不掉任何洞。**
今天每个用户只有一个自己拥有的渠道空间，跨租户读写渠道行本来就不可能。

**真正的提权面在另一侧**：前端下拉按团队口径列目标（`canvas_service.py:177-181`、
`dialog_service.py:283`，都含 `joined_tenant_ids` + `permission=TEAM`），后端只认个人口径
（`api/channel_control/repository.py:141,157`）。于是团队共享的 Agent 出现在下拉里、被选中、
被拒绝，而拒绝原因又被前端三处裸 `catch` 吞掉——这是**今天就存在的可复现缺陷**。

| 方案 | 结论 |
|---|---|
| A. 九条路由补 `can_manage_*` | ❌ 恒为 True，无效 |
| B. 只放宽后端查询到团队口径 | ❌ 任意 `normal` 成员就能把同事的 Agent 接到整个飞书组织，且 `principal_id=None` 无法追责 |
| C. 只收窄前端下拉到个人口径 | ❌ 删掉大家正在用的功能 |
| D. 放宽查询 **且** 按目标的归属租户判 `can_update_tenant_resources` | ✅ 采纳 |

**决策**：渠道保持个人租户资源。授权只加在 binding target 上，按**目标的归属租户**判角色，
复用既有的 `can_update_tenant_resources`（`user_service.py:276-277`，`{OWNER, ADMIN}`）。
不加新角色、不加新列、不做前端导航隐藏。

- `DELETE /{id}`、`POST /{id}/disable` 只查行归属——**故障安全方向永远不能被挡**。
- `POST /chat-channels`、`PATCH /{id}`（带 binding 或 chat_id 时）、`PUT /{id}/binding`、
  `POST /{id}/enable` 查行归属 **且** 目标授权。
- `canvas_revision_is_latest_published` 同时返回归属租户，让「不是你的」与「版本过期」
  成为两个不同的错误码。

**前端导航不按角色隐藏**：这个页面是每个用户都合法拥有的个人空间，真正的限制是逐目标的、
只在绑定时才可知；`SettingsLayout` 也没有这个角色信号，加一个等于每次设置页渲染都多一次请求，
去编码一条并不属于这个页面的规则。限制呈现在它真正成立的地方——目标下拉只列可绑定的，
错误码内联渲染在目标字段上。**这也意味着本决策产生零跨仓顺序约束。**

**存量迁移**：`list_desired_runtimes` 刻意**不**重跑目标授权，所以 API 部署不会打掉正在跑的
生产渠道（那会是没有前端信号的坏半态）。检查在下一次写操作时才咬人。CHN-S6 的脚本先枚举
不合规 binding 供运维处置。无数据迁移、无 backfill、无停机。

**明确不解决**：`api/channel_execution/adapters.py` 的 `principal_id=None`——渠道消息执行时
没有终端用户归属。缝已经存在（`TrustedChannelContext.principal_id: str | None` 一路串到
`user_id=principal_id or ""`），将来做身份映射不需要契约变更。那是独立的、更大的一件事。

---

## CHN-ADR-02 · 运行时状态走自适应轮询，否决 SSE / WebSocket

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：上游对同类需求用 1 秒裸 `setInterval` 轮询；「换成 SSE」是本能反应。

**决定性的反向事实**：运行状态写在 Postgres 的 `t_ai_channel_runtime_status`，
**没有任何 pub/sub 通知**。一个 SSE 端点为了知道状态变了，只能自己在服务端轮询那张表——
于是 SSE 等于把轮询搬进服务端，还额外背上长连接状态管理。严格更差。

而且数据本身的变化率就不支持：`runtime_heartbeat_seconds` 默认 15、
`reconcile_interval_seconds` 默认 10。前端当前已经是 `refetchInterval: 15 * 1000`，
不是上游那种 1 秒裸定时器，所以「换传输」能拿到的延迟收益接近零。

**决策**：不做 SSE / WebSocket / long-poll。真正该花钱的地方是：
① 删掉冗余的 `/runtime` 查询（列表响应里 `include_runtime=True` 已经带了 runtime）；
② 瞬态状态下把 `refetchInterval` 降到 3 秒，`document.hidden` 时停；
③ 可选地给单渠道 `/runtime` 加 ETag。

**代价**：状态最坏仍有 15 秒延迟。在 15 秒心跳面前这不是真实成本。

**这条决策的失效条件**（写在这里，将来不用重新论证）：如果引入扫码配对类 provider
（二维码 20 秒级有效期），这个判断需要重做——那时正解是 SSE，不是把轮询间隔调到 1 秒。

---

## CHN-ADR-03 · 服务端展平 FieldSpec，前端不编译 JSON Schema

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：`GET /chat-channels/providers` 已经下发 `config_schema`，前端号称 schema 驱动。
实测 `FeishuConfigInput.model_json_schema()` 之后发现它**表达不了渲染所需的信息**：

- 根级与 `$defs` 里**都没有 `required` 数组**——所有字段带默认值，因为 PATCH 需要 merge 语义；
- `app_secret` 的 `format:"password"` 埋在 `anyOf[0]` 里（Optional 包装的后果）；
- placeholder、排序、分组、i18n、跨字段规则一样都表达不了。

所以前端硬编码 `new Set(['app_id','app_secret'])` 不是偷懒，是在补服务端表达力的缺口——
而且补的方式是撒谎：客户端声称必填的两个字段，服务端 schema 说全都可选。

| 方案 | 结论 |
|---|---|
| A. `@rjsf/core` + ajv8（2026 最主流答案） | ❌ ajv8 约 40KB gz 是与 zod 并存的第二套校验引擎，而前端表单栈被 `AGENTS.md` 钉死为 react-hook-form + zod；要过 bundle 三道闸；**且 `uiSchema` 本质是客户端的 per-provider 配置——引进来等于把刚删掉的 provider 知识换个名字请回前端** |
| B. 前端自研 JSON Schema 子集编译器 | ❌ `$ref` 解析 + `anyOf` 折叠 + `oneOf` 分支求值必然突破 600 行文件棘轮，且要长期维护一份「我们支持哪些关键字」的契约 |
| C. 服务端展平成有序 FieldSpec，前端只排序/过滤/分桶 | ✅ 采纳 |

**决策**：manifest 同时下发两份派生物——`config_schema`（Pydantic 自动生成，**只**服务请求校验
与 OpenAPI）与 `form.fields`（服务端展平的有序 FieldSpec，**只**服务渲染）。
前端不解析 JSON Schema。`required` 落在 form 层，`FeishuConfigInput` 因此名正言顺地保持
全字段可选以支持 PATCH merge。

**工业界对照**：这条路等于 **Airbyte 的后端 + n8n 的前端契约**。分界线是「你的前端能不能养一个
schema 引擎」：养得起的（Airbyte `connectionSpecification` + `airbyte_secret`/`order`/`group`、
RJSF + Backstage、K8s CRD + `x-kubernetes-*`）走 JSON Schema 扩展；养不起的
（n8n `INodeProperties` 的 `displayOptions.show|hide`、Zapier `inputFields`、Nango/Paragon）
走服务端拍平的描述符。本仓属后者。顺带说明：Airbyte 自己的 webapp 也没用 rjsf，
它手写了 `ServiceForm` 把 spec 编译成表单字段。

**代价**：manifest 里有两份派生物，可能写歪。缓解是参数化的一致性测试——每个标了 secret 的
模型字段必须有对应 `bucket=secret` 的 form 字段且反之亦然，每个 `form.path` 必须在
`config_model` 里可解析。**这条测试跑不到就会静默漂移**：`common/data_source/` 那 5 个
「后端有、前端无」的连接器就是没有这类测试的下场。

**留缝**：`FormField.kind` 是**开放联合**，前端渲染未知 kind 为 disabled 字段而非抛错。
将来加企微的 `visible_when`、加 OAuth 按钮时，老前端因此能优雅降级。这是 CHN-P12
（交互式配对）的全部留缝成本。

---

## CHN-ADR-04 · leader lease 用 binding 维度而不是租户维度

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：`api/channels/state_store.py:117` 的 Redis 命名空间是 `hash(app_id)`，不含任何租户
维度；`api/channels/worker.py:168` 在 `:177 await self._channel.start()` 校验凭据**之前**就
抢租约。`app_id` 是非机密标识、数据库对它无唯一约束、`_ensure_ready` 只查非空——
所以租户 B 拿租户 A 的 app_id 配任意假 secret 建渠道并启用，就能抢到同一把 lease，
让租户 A 的 worker 在任何一次重启后以 `LEADER_LEASE_HELD` 起不来。可利用的跨租户拒绝服务。

**直觉修法是把 `tenant_id` 加进命名空间。否决它的硬事实**：worker 手上根本没有 `tenant_id`——
`RuntimeBindingConfig`（`api/channel_runtime/schemas.py:34-41`）不携带它。加进去意味着往一个
`extra="forbid"` 的私有契约模型加字段，按 [CHN-ADR-06](#chn-adr-06--私有-runtime-契约的每次变更都拆成-tolerate--emit-两个-pr)
就要拆成两个 PR、中间夹一次运行时部署——**把一个必须现在上线的安全修复变成两次部署的协议升级。**

**决策**：命名空间改成 `("binding", binding_id)`。`binding_id` 已在 worker 手上、全局唯一、
按构造即租户隔离，零契约变更。跨租户抢占在结构上不再可能。

**白送的收益**：同一个 `app_id` 重建渠道会拿到全新的 dedupe 命名空间——原本
「删渠道不清 Redis、重建后老 message_id 被判重复而静默丢消息」那个 bug 一并消失，
不需要单独做 Redis 清理。

**代价**（必须写进 PR body 与 `api/channels/README.md`）：接住这次改动的那一次 worker 重启，
用户的 Agent 会话重置一次（重新开始一轮对话），dedupe 窗口空一次（一条在途消息可能被回答两次）。

**引出的新约束**：lease 变成 per-binding 后，同一租户内两个 binding 绑同一个飞书 app 会**同时**
连上并重复回答。所以唯一性不变量必须上移到控制面（CHN-S4），且**只在租户内**检查——
全局唯一性检查本身就会变成新的跨租户抢占面（B 先注册 app_id X，A 永远被锁在外面）。

---

## CHN-ADR-05 · 文档分层：入库讲我们的代码，本地讲别人的代码

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：两个约束互相拉扯。一边是「未来零上下文的 agent 要能从这些文件启动」——指向入库；
另一边是本仓刻意的本地笔记政策（`.gitignore` 的 `internal/*.md`，提交 `c847a03b` 主动取消
跟踪了那批笔记），以及先前明确要求某些 channel/上游对照知识不要写进仓库文档。
凭感觉划这条线会得到一个维护不了的边界。

**决策**：用一条可判定的轴——

> **入库 = 关于我们自己代码的陈述。本地 = 上游/第三方对照分析，以及未修复漏洞的复现级细节。**

桥接规则：**入库的任务行携带 `file:symbol` 锚点与要建立的不变量；本地文件携带攻击手法。**
检验一下这在操作上是否免费：CHN-S3 的任务行写的是「`api/channels/state_store.py:117` 的
`_namespace` 缺租户维度，且 lease 先于凭据校验获取」——这**完全足够动手修**。
被扣下的只是复现攻击的配方。所以冷启动的 agent 在一个新 clone 上什么都不缺；
而仓库一旦外泄，也不会连带交出四份可用的攻击步骤，并永久留在 git 历史里。

**落地**：
- 入库：`docs/channel-program/{README,PROGRESS,DECISIONS,CONTRACT}.md`（需先修 `.gitignore`，见下）
- 本地：`internal/channel-audit-2026-08.md`

**为什么是改 `.gitignore` 加白名单，而不是 `git add -f`**：`.gitignore:232` 原本是裸 `docs`，
排除的是**目录**——git 无法在被排除的父目录下重新包含文件，所以直接追加 `!docs/channel-program/`
会**静默失效**。必须先改成 `docs/*`。而 `git add -f`（`docs/references/http_api_reference.md`
当年就是这么进来的）是更坏的先例：文件*看起来*被跟踪了，于是下一个人在旁边新建文件、提交，
那个文件对所有人静默不存在。白名单是自解释的、`git status` 看得见的，也让本仓的 gitignore
结构与 web 仓（`docs/*` + `!` 行）一致。

**同批必须把 `!docs/references/` 也加上**——否则那个目录只是因为已经在 index 里才继续工作，
下一个在那儿建文件的人会踩空。

---

## CHN-ADR-06 · 私有 runtime 契约的每次变更都拆成 tolerate + emit 两个 PR

**日期**：2026-08-05 · **状态**：✅ 采纳

**背景**：`DesiredRuntime` / `DesiredRuntimeList` / `RuntimeCredential` / `RuntimeBindingConfig` /
`RuntimeReport` 全是 `extra="forbid"`。supervisor 与 worker 是长驻进程，**API 部署不会重启
它们**——而且 `docker/docker-compose.yml` 里压根没有 supervisor 服务（CHN-O5 才补上），
它今天是手工/外部托管的。所以两侧永远假定在不同的提交上。

**硬事实**：`extra="forbid"` 会把一个未知键变成**整次调用的解析失败**。对 `DesiredRuntimeList`
来说，那不是「新 binding 起不来」，是 `supervisor.py:96-101` 记一条 warning 就跳过**整轮**
reconcile——所有 binding，包括健康的飞书 binding，都不再被拉起也不再被回收。

**决策**：这五个模型的每一次变更都拆成两个 PR，中间夹一次运行时部署。教会*消费方*接受新形状
的 PR 先合并并部署到位（tolerate），之后让*生产方*发出它的 PR 才能合（emit）。
**一个 PR 同时做两件事就是协议破坏，不管那个字段看起来多「加法」。**

- 方向决定谁先动：`DesiredRuntime`/`RuntimeBindingConfig`/`RuntimeCredential` 由 API 发、
  supervisor/worker 收（消费方先动）；`RuntimeReport` 反过来。
- **放宽 `Literal` 也是 tolerate-then-emit**，只是发生在值域。
- **删字段是三步**：停止读 → 停止发 → 删。
- **能在控制面合成的语义变更豁免**（无 schema 变更）——CHN-O1 就是刻意选了这条路，
  否则 worker 得懂 Canvas 发布语义，还要两次部署，只为修一个显示问题。
- **绝不把 `extra` 改成 `"ignore"` 来绕开这条规则**：tolerate PR 的意义正是让 `forbid` 重新安全，
  而 `forbid` 是唯一能抓住拼写错误的性质。

**流程强制**：每个 emit PR 的 body 必须写明它对应的 tolerate PR 与最低 supervisor/worker 版本，
以及运维确认版本的命令。**没写就打回。**

### 补充（2026-08-05，CHN-P4 实测补上）：tolerate 步不得改变线格

原文说的是「消费方先接受新形状」，但漏了一个前提：**这些模型被生产方和消费方共用**。
往 `RuntimeCredential` 加一个带默认值的字段，FastAPI 序列化响应时就会立刻把
`"fields": {}` 放到线上——而没升级的 worker 用 `extra="forbid"` 解析这个响应，
会**整包拒绝**、binding 永远起不来。也就是说，一个「只加字段没填值」的 tolerate 步
本身就是破坏性变更。

所以规则补一条：

> **tolerate 步落地后，线格必须逐字节不变。** 共用模型加了字段，就要在**路由层**
> 排除它（`response_model_exclude={"credential": {"fields"}}`），直到 emit 步才放开。
> 验收方式是既有的线格断言**不加修改地继续通过**——CHN-P4 正是被
> `test_runtime_config_releases_only_provider_connection_material_to_authenticated_runner`
> 抓住的，那条测试断言的是完整响应体相等。

这条不是理论推演：CHN-P4 第一版就踩了，测试当场变红。

**一条能缩小整个问题的运维安排**：CHN-O5（supervisor 进 compose）刻意排在两个 tolerate 步
（CHN-P4、CHN-O2）之后。对任何今天没跑 supervisor 的部署——按 compose 现状那是**默认情况**——
它跑起来的第一个 supervisor 就已经越过了两个 tolerate 步。这条规则因此只约束当前手工运行
supervisor 的少数环境。

---

## CHN-ADR-07 · Provider 与执行目标正交，历史事务由目标驱动拥有

**日期**：2026-08-10 · **状态**：✅ 采纳

**背景**：CHN-U9 为连续追问、取消和重新生成建立了正确的安全底线：生成成功且有可见答案前，
不得污染公开历史。首期实现让 MultiRAG Dialog 和 MultiRAG Canvas 都在私有数据库候选上执行，
再由 `ChannelSessionManager` 原子晋升。这个方案修复了线上错误，但若把它固化成最终抽象，会出现
三个长期问题：

1. `ChannelSessionManager` 每增加一个目标就增加一组 `prepare_xxx/complete_xxx`，不是可扩展端口；
2. Dialog 本身可以对 detached transcript 生成，仍创建数据库候选造成不必要写放大；
3. Canvas completion 当前会自行持久化，不能因为 Dialog 可无落库就直接删掉 Canvas 隔离层。

同时，后续会增加多个 Provider，而 MultiRAG 的主适配目标仍是 Dialog，Canvas 只是本项目额外支持。
如果历史策略落在飞书 worker 或 Provider bridge，未来每个 Provider 都会复制一份 Dialog/Canvas
分支，跟进上游也会不断触碰同步区。

### 方案比较

| 方案 | 结论 |
|---|---|
| A. 给 MultiRAG 同步区的 Dialog/Canvas completion 增加 Channel 私有 flags | ❌ 上游每次同步都冲突，且把 transport 生命周期泄漏进业务核心 |
| B. 所有目标永久共用数据库候选 | ❌ 表面统一，实际把 Canvas 的限制强加给 Dialog，并让新目标继承无谓写放大 |
| C. 每个 Provider 自己处理历史、重新生成和取消 | ❌ Provider × Target 组合爆炸，worker 被迫理解数据库和业务副作用 |
| D. Provider/Target 正交；每个目标驱动实现相同生命周期、不同提交策略 | ✅ 采纳 |

**决策**：

- Provider 只拥有 inbound/outbound、ReplySession 和交互回调；目标执行、历史投影、CAS 和副作用
  判定属于 `api/channel_execution`；
- 保留 `TargetExecutorRegistry`，在执行器内部引入目标私有 driver；公共 Protocol 不出现
  `prepare_canvas()` 或 `complete_dialog()`；
- Dialog 为默认主目标，采用内存工作副本 + 终态单次 CAS（`detached_cas`）；
- Canvas 是扩展目标，在获得无落库端口前采用专属候选 + 终态 CAS（`candidate_cas`）；候选回收
  离开请求热路径；
- 用户可见操作由 Provider capabilities、Target capabilities 与 run policy 取交集；
- worker 不直连数据库，继续只通过 MultiRAG 内部 API/SSE；不得按 token 或卡片 patch 读写数据库；
- 本仓表、函数、方法和运行时一律使用 MultiRAG 口径；外部项目名只用于来源与差异说明。

完整组件关系、I/O 预算、重新生成语义和实施拆分见
[`EXECUTION_ARCHITECTURE.md`](EXECUTION_ARCHITECTURE.md)。

**为什么现在不直接引入完整 durable workflow runtime**：DeerFlow/LangGraph 一类实现证明，跨进程
恢复需要 run ledger、lease、checkpoint 和取消/终态 CAS；但 MultiRAG 当前首期只承诺进程内队列，
且同步核心不是 LangGraph thread。先引入整套运行时会把“去掉 Dialog 候选写放大”变成平台重写。
本决策只固定不可逆的边界；持久化 run ledger 由 CHN-O14 在真实恢复需求成立后独立评审。

**失效条件**：如果未来 MultiRAG Dialog/Canvas 都改为同一个原生 checkpoint runtime，且该 runtime
同时提供无副作用 fork、可见历史投影和条件提交，则两个 driver 可以共享其实现；Provider/Target
正交、worker 无数据库和能力交集三条仍不失效。

---

## CHN-ADR-08 · Canvas 与 Channel 演进以上游同步为主，只在适配层吸收现代执行不变量

**日期**：2026-08-10 · **状态**：✅ 采纳 · **补充**：CHN-ADR-07

**背景**：CHN-U14/U15 已证明 Dialog 与 Canvas 可以对外遵守同一套
`prepare -> execute -> publish/abort` 语义，但当前实现方式不同。Dialog 有不持久化会话的生成缝，
可以使用内存工作副本；Canvas completion 仍自行创建、读取和写回会话，因此需要私有 candidate
和 sidecar 隔离公开历史。与此同时，MultiRAG 会持续跟进 RAGFlow 的 Canvas、Agent 和 Channel
源码，不计划另造一套与上游长期并行的 Canvas runtime。

LangGraph、DeerFlow、Open WebUI 和同类现代项目提供了 run/thread 分离、checkpoint、取消与终态
竞争 CAS 等有价值的校准基线，但它们不是要求 MultiRAG 替换 RAGFlow Canvas 内核的理由。需要固定
的是一致性和安全不变量，而不是照搬某个项目的存储表或框架。

**决策**：

1. **RAGFlow upstream-first。** MultiRAG Canvas/Agent 核心和 Channel transport 持续按上游 commit
   迭代；每次升级先固定上游 SHA、审计实际差异，再按路径所有权语义移植。不得因本地架构偏好复制
   或分叉完整 Canvas completion、Agent 编排和 checkpoint runtime。
2. **优化留在反腐层。** Channel 的终态发布、CAS、operation、能力协商、Provider 渲染和候选所有权
   优先落在 `api/channel_execution`、`api/channel_providers` 及 MultiRAG 自有表；上游同步区不增加
   Channel 私有参数、历史分支或 Provider 判断。
3. **长期只固定不变量。** 中间执行状态不冒充公开历史；完整可见终态才发布；并发发布必须 CAS；
   run 状态与公开消息分离；未知或外部副作用不得自动重放；Provider 不理解目标数据库；worker 不
   直连数据库。这些规则独立于 `detached_cas`、`candidate_cas` 或未来上游 runtime 的具体名称。
4. **Canvas 按上游实际形态条件演进。** 若上游继续自持久化，保留 U15 candidate bridge；若上游
   提供稳定 no-store/snapshot 执行缝，再评估在目标 driver 内切换 detached CAS；若上游提供原生
   run/checkpoint/resume，则优先适配该原生模型并评估删除 candidate，而不是并行维护本地 runtime。
   任一路径切换前都必须证明历史、发布版本、取消、并发和工具副作用语义等价。
5. **两类运行状态不得混淆。** Canvas 内部 workflow checkpoint 属于目标执行状态；CHN-O14 的
   Channel run ledger 属于跨 Provider 的排队、取消和交付协调。U15 sidecar 只证明候选所有权，
   三者不能互相冒充。O14 仍只在明确需要跨进程恢复时启动。
6. **本地分叉必须可退出。** 每项偏离上游的实现都要记录上游 SHA/路径、本地落点、保护的不变量、
   契约测试和删除条件；上游出现等价或更好的原生能力时，优先收敛并删除兼容债。

### Canvas 演进决策门

| 上游事实 | MultiRAG 选择 | 禁止项 |
|---|---|---|
| completion 继续自行持久化会话 | 保留 candidate + sidecar + terminal CAS | 为追求形式统一直接写公开历史 |
| 出现稳定 no-store/snapshot 执行缝 | 先做语义等价审计，再评估 driver 内 detached CAS | 复制整段 completion 建平行入口 |
| 出现原生 run/checkpoint/resume | 适配上游目标 runtime，终态仍投影到公开历史 | 同时维护本地和上游两套 checkpoint runtime |
| 跨进程恢复成为真实 Channel 需求 | 独立评审 CHN-O14 run ledger | 把 Canvas sidecar 当 durable run ledger |

**执行方式**：EIM-F5 / CHN-X14 当前挂起。触发条件是 Channel 已稳定，且用户恢复从约 4 月 24 日
上游基线逐 commit 跟进；届时审计随正常同步节奏进行，并给出“直接跟进 / 语义移植 / 适配层吸收 /
暂不采纳”清单。只有识别出稳定、可验证的执行缝后，才新增代码任务。审计本身不授权重写 Canvas，
也不自动启动 CHN-O14。

这一排期变化不修改本 ADR 的 upstream-first 长期原则。U15 数据库迁移与 API/supervisor 重启已经
完成，近期先补 Dialog/Canvas 现场 smoke，再做 CHN-U16 的正常优雅停机终态化，随后实施 CHN-O9
可观测。
CHN-U16 只覆盖进程能够协作清理的 queued/running 回复，不包含 kill -9、进程崩溃或跨实例恢复。
