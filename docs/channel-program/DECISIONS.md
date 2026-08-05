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

**一条能缩小整个问题的运维安排**：CHN-O5（supervisor 进 compose）刻意排在两个 tolerate 步
（CHN-P4、CHN-O2）之后。对任何今天没跑 supervisor 的部署——按 compose 现状那是**默认情况**——
它跑起来的第一个 supervisor 就已经越过了两个 tolerate 步。这条规则因此只约束当前手工运行
supervisor 的少数环境。
