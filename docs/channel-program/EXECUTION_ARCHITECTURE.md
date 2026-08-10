# MultiRAG Channel 执行架构

> 状态：EIM-U11 / CHN-X13 已实现；U14 / U15 按目标私有事务继续演进
> 决策：[`CHN-ADR-07`](DECISIONS.md#chn-adr-07--provider-与执行目标正交历史事务由目标驱动拥有)
> 核验日期：2026-08-10（外部版本与证据见
> [`VERSION_BASELINE`](../enterprise-identity-mcp/VERSION_BASELINE.md) 和
> [`REFERENCES`](../enterprise-identity-mcp/REFERENCES.md)）

本文是 Channel Provider、MultiRAG Dialog、MultiRAG Canvas 之间执行关系的单一事实来源。
飞书卡片、Markdown 和回调时序仍以
[`FEISHU_BOT_UX`](../enterprise-identity-mcp/FEISHU_BOT_UX.md) 为准；任务状态以
[`PROGRESS`](PROGRESS.md) 和 [`ROADMAP`](../enterprise-identity-mcp/ROADMAP.md) 为准。

## 1. 术语与项目口径

仓库内一律使用 MultiRAG 自己的实体名称：

- **MultiRAG Dialog / MultiRAG Canvas**：本项目的两类执行目标；Dialog 是主目标，Canvas 是扩展目标；
- **MultiRAG 对话表、会话表、函数、方法、路由、执行器**：只要实体位于本仓库，就按 MultiRAG
  命名，不称为“RAGFlow 的表/函数/方法”；
- **上游同步区**：为了降低后续同步成本而保持低差异的 MultiRAG 文件；它仍是 MultiRAG 代码，
  不是外部运行依赖；
- **RAGFlow 上游**：只在说明代码来源、许可证、外部 commit/PR、差异对比或兼容基线时使用。

不得用“RAGFlow completion”“RAGFlow 会话表”“调用 RAGFlow 数据库”描述本仓运行时。正确说法是
“MultiRAG Dialog completion”“MultiRAG Canvas 会话表”“MultiRAG API 访问本地数据库”；如需说明
来源，再单独补一句“该实现跟随 RAGFlow 上游某提交”。

## 2. 定案结论

1. **Provider 与执行目标是两条正交轴。** 飞书、钉钉和后续 Provider 只负责事件归一化、回复渲染
   和交互回调；Dialog、Canvas 和后续目标只负责业务执行与历史事务。
2. **Dialog 是默认和首要适配目标，Canvas 是独立扩展。** 新增 Provider 必须天然可绑定 Dialog；
   Canvas 能力不得反向成为 Provider 通用契约的必填项。
3. **所有 Provider 共用同一个 MultiRAG 执行入口和事件协议。** 禁止出现
   `provider == "feishu" and target_type == "multirag.canvas_agent"` 一类组合分支。
4. **运行中的内容不是已提交历史。** 生成只在私有工作态进行；只有完整终态、存在可见答案且公开
   会话头未变化时，才以 compare-and-swap（CAS）提交一次。
5. **历史事务由目标驱动拥有，不追求伪通用。** 当前两者仍保持原候选语义；U14 将 Dialog 改为
   内存工作副本，Canvas 继续使用隔离候选会话。二者共用生命周期语义，不共用持久化技巧。
6. **Channel worker 不访问数据库。** worker 只调用 MultiRAG 内部执行 API 并消费 SSE；数据库访问
   只发生在 API 进程的目标驱动中，卡片 patch 和模型 delta 不触发数据库轮询。
7. **功能由能力交集决定。** 用户可见的停止、重新生成、反馈和渐进式展示，取 Provider 能力、
   目标能力与本次执行策略的交集；不支持时明确降级或隐藏，不能猜测。

## 3. 当前事实与需要继续收口的地方

已经正确的基础：

- `api/channel_execution/registry.py::TargetExecutorRegistry` 已按 `target_type` 注册目标执行器；
- `SqlAlchemyDialogTargetDriver` 与 `SqlAlchemyCanvasTargetDriver` 分别持有目标私有 history transaction，
  公共协议不再出现目标名称；
- `api/channels/runtime_client.py` 通过内部 HTTP/SSE 调用执行 API，worker 不直接访问 SQLAlchemy；
- managed worker 启动时为当前 binding generation 读取一次脱敏能力信封，进程重启允许重新读取；消息、
  delta 和卡片 patch 均不读取目标数据库；
- `ExecutionEvent` 与 `ReplySession` 已把目标输出和 Provider 渲染分开；
- binding 在服务端解析 tenant、target、revision 和 session，worker 不能覆盖这些字段。

EIM-U11 / CHN-X13 已收口的部分：

- 原 `ChannelSessionManager` 已删除，替换为泛型 `TargetHistoryTransaction[PreparedT]`；Dialog 与 Canvas
  分别拥有不透明 prepared 类型和 `prepare/commit/abort` 实现；
- Provider、Target 与 RunPolicy 通过纯函数求交；Bridge 只签发有效 action ID，渲染与回调均二次校验；
- 完成后的 regenerate 与失败/取消后的 retry 已分开建模；未解析的 conditional capability、旧 API、
  预取异常和未知 Canvas 组件均 fail closed；
- 飞书在 `progressive_reply=false` 时回退 buffered reply，而不是继续假装流式能力存在。

后续 U14 / U15 需要演进的部分：

- 两个目标私有 history transaction 目前都创建数据库候选，虽然保证了失败、取消、reasoning-only
  不污染公开历史，但对 Dialog 来说写放大并非必要；
- 候选 TTL 清理目前位于每次执行的准备热路径，会为每条消息增加一次删除扫描和提交；
- Canvas 候选依靠会话行中的名称/用户字段识别，能工作但不是理想的长期所有权模型。

不能据此直接删除现有隔离层。当前 MultiRAG completion 在内部流结束时可能已经写入空答案、仅推理
内容或部分状态；在无副作用执行缝建立前改为直接写公开会话，会重新引入历史污染。

## 4. 两条正交轴

```mermaid
flowchart LR
    subgraph P["Provider 轴"]
        F["飞书"]
        D["钉钉"]
        N["后续 Provider"]
    end

    F --> B["BindingBridge + ReplySession"]
    D --> B
    N --> B
    B --> A["MultiRAG 内部执行 API"]
    A --> R["TargetExecutorRegistry"]

    subgraph T["执行目标轴"]
        DG["MultiRAG Dialog Driver\n主目标"]
        CV["MultiRAG Canvas Driver\n扩展目标"]
        NX["后续目标 Driver"]
    end

    R --> DG
    R --> CV
    R --> NX
    DG --> E["Provider-neutral ExecutionEvent"]
    CV --> E
    NX --> E
    E --> B
```

Provider 不知道 MultiRAG 对话表结构，目标驱动也不知道飞书 CardKit 或钉钉 webhook。二者唯一的
交点是受信执行命令、能力描述和脱敏后的执行事件。

## 5. 目标驱动契约

注册表继续以 `TargetExecutor` 为入口；具体 driver 在执行器内部拥有目标私有 history transaction。
当前公共形态为：

```python
class TargetExecutor(Protocol):
    target_type: str

    async def capabilities(self, context: TrustedChannelContext) -> TargetCapabilities: ...
    async def execute(
        self,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]: ...


class TargetHistoryTransaction[PreparedT](Protocol):
    async def prepare(...) -> PreparedT: ...
    async def commit(self, prepared: PreparedT, generated_session_id: str) -> str: ...
    async def abort(self, prepared: PreparedT, generated_session_id: str | None) -> None: ...
```

`PreparedCanvasExecution` 与 `PreparedDialogExecution` 是各自 driver 的私有工作态。对应实现是
`SqlAlchemyCanvasHistoryTransaction` 和 `SqlAlchemyDialogHistoryTransaction`；公共 Protocol 不再出现
`prepare_canvas()`、`complete_dialog()` 等具体目标方法。新增目标只注册新 executor/driver，不修改
既有 Provider。

## 6. 能力协商

### ProviderCapabilities

| 能力 | 含义 |
|---|---|
| `progressive_reply` | 可以渐进展示同一回复 |
| `interactive_actions` | 可以承载按钮或等价交互 |
| `cancel_control` | 可以向用户展示停止操作 |
| `feedback_control` | 可以承载有帮助/没帮助 |
| `threaded_reply` | 可以稳定回复到来源消息或话题 |

### TargetCapabilities

| 能力 | 含义 |
|---|---|
| `streaming` | 目标能产出增量事件 |
| `cancellable` | 当前阶段可合作取消；不代表副作用回滚 |
| `regeneration` | `always`、`conditional` 或 `never`；只有解析后的 `always` 可展示重新生成 |
| `retryable` | 失败/取消且未提交的 run 是否可安全重试；与 regenerate 分开 |
| `feedback` | Channel 可接收本轮低风险反馈；当前是卡片确认与脱敏事件，不宣称已有持久反馈仓 |
| `commit_mode` | `detached_cas`、`candidate_cas` 或未来模式 |
| `effect_class` | `generation_only`、`external_effects`、`unknown` |

最终 UI 能力为：

```text
visible_actions = ProviderCapabilities ∩ TargetCapabilities ∩ RunPolicy
```

Dialog 当前为 `candidate_cas`，由 U14 迁移到 `detached_cas`。Canvas 的 `regeneration` 与 `retryable`
按已发布图动态判断：含工具、MCP、代码执行、写 SQL、未知组件、文档/Excel 持久输出，或带附件输出/
Memory 保存的 Message 时均 fail closed；纯文本 Message 可安全重放。停止按钮只表示停止等待后续输出，
不得宣称已撤销外部副作用。

能力通过 workload-authenticated 的
`GET /api/v1/internal/channel-bindings/{binding_id}/execution-capabilities` 下发。响应只含最终布尔能力，
不含 target type/id/revision、图结构、`commit_mode` 或 `effect_class`。worker 在每次启动时为当前 binding
generation 读取并缓存一次；同 generation 的崩溃重启会重新读取，正常消息、token、CardKit patch 不读取。
旧 API、超时或非法响应时继续正常回答，但关闭渐进式和所有交互动作。regenerate/retry 在执行 API
claim event 之前还会按 Target 与 RunPolicy 再授权；隐藏按钮不是唯一保护。

## 7. 历史事务模型

### 共同语义

```mermaid
stateDiagram-v2
    [*] --> Snapshot: 读取公开头和可见历史
    Snapshot --> Running: 构造私有工作态
    Running --> Validate: 收到完整终态
    Running --> Discarded: 失败或取消
    Validate --> Committed: 可见答案 + 公开头 CAS 成功
    Validate --> Discarded: 空答案、仅推理或并发冲突
    Committed --> [*]
    Discarded --> [*]
```

- 公开历史只保存用户可见的 user/assistant transcript、引用和允许的结构化产物；
- reasoning、原始 tool trace、半轮回答、失败和取消结果不进入公开历史；
- `message` 在快照尾部追加问题；`regenerate` 在工作副本中撤回最新且问题匹配的一轮；
- 最终提交必须携带 `expected_head`（版本号或稳定指纹），条件不成立返回冲突，不覆盖后来消息；
- 公开会话只在终态写一次。流式事件、卡片刷新和反馈渲染不写对话历史。

### Dialog：`detached_cas`

Dialog 是主目标，应先收敛到：

1. 读取一次 MultiRAG Dialog 公开会话和目标配置；
2. 清洗为可见 transcript，在内存中构造 detached working copy；
3. 调用 MultiRAG Dialog 生成能力，但不让生成过程写公开会话或候选行；
4. 完整终态后，在一个事务中按 `expected_head` 更新公开会话；新会话则到终态才插入；
5. 失败、取消、仅推理或冲突时丢弃内存副本，公开历史零写入。

这与 MultiRAG 后续跟进上游时的方向一致：同步区保持原有 Dialog 语义，Channel 的 operation、
CAS 和可见历史投影都留在 `api/channel_execution`。禁止给同步区函数新增 Channel 私有参数。

### Canvas：`candidate_cas`

当前 MultiRAG Canvas completion 自己读取和持久化会话，尚无等价的无落库执行端口，因此：

1. 保留数据库候选作为**Canvas 专属兼容策略**，不再伪装成所有目标的通用方案；
2. 候选所有权、创建时间和状态由 MultiRAG 自有元数据记录；公开 Canvas 会话表不增加 Channel
   私有列；
3. 成功后锁定公开行并校验 `expected_head`，再复制允许字段并删除候选；失败只删除候选；
4. 候选 GC 改为启动时/周期性低频批处理，不在每条消息准备路径执行；
5. 一旦 MultiRAG Canvas 获得稳定的无落库执行端口，驱动内部切到 `detached_cas`，Provider、
   binding、事件协议和卡片均不变。

## 8. Run 生命周期与会话历史分离

每次尝试拥有新的 `run_id`。重新生成创建新 run，并记录 `supersedes_run_id` 或等价来源关系；它
不是把旧问题当普通消息再次追加。`queued/running/final/error/cancelled` 是 run 状态，不是 Dialog
或 Canvas 会话消息。

当前首期可继续由 worker 持有有界队列，因为它能以最低延迟驱动 Provider 卡片；但这只是进程内
体验状态。若要支持 worker 重启恢复、跨实例取消或长期可查询状态，按 CHN-O14 增加 API 侧持久化
run ledger：

- worker 仍只通过内部 API/SSE，不直连数据库；
- 每个会话最多一个 `queued/running` run 的约束由数据库唯一条件兜底，内存检查只是快路径；
- 取消请求和成功终态使用竞争 CAS，已接受的取消不能被迟到 success 覆盖；
- run 事件和目标历史分表，不能把运行状态塞进 Dialog/Canvas 消息数组；
- 不按 token 写 run 表，只在有限状态迁移和终态写入。

在明确需要重启恢复前，不提前引入完整 durable workflow runtime；MultiRAG 只吸收其一致性不变量。

## 9. 数据库 I/O 预算

| 路径 | 目标预算 | 禁止项 |
|---|---|---|
| Dialog 已有会话 | 准备阶段一次快照读；终态一次 CAS update | 候选 insert/delete、每 delta 写库 |
| Dialog 新会话 | 目标配置读；终态一次 insert | 开始生成就发布空会话 |
| Canvas 已有会话 | 快照读 + 候选写；终态 CAS copy/delete | 每请求全表/目标范围 TTL 清理 |
| Canvas 新会话 | 私有候选创建；终态发布映射 | 半成品直接成为公开会话 |
| capability preflight | 每次 worker 启动、每个 binding generation 一次脱敏读取；动作时按目标安全性再授权 | 每消息、每 token、每卡片 patch 查询 |
| Provider 渲染 | 0 次目标历史 SQL | 为卡片 patch 轮询数据库 |
| run ledger（后续） | 状态迁移时小次数 CAS | 每 token、每 CardKit sequence 写库 |

desired runtime 的控制面轮询是另一条运维链路，不属于对话执行热路径，不能与上述 I/O 混为一谈。

## 10. 重新生成的产品语义

Dialog 和 Canvas 当前都是线性会话，因此只开放“重新生成最新完成轮次”：

1. 新建 run；
2. 以原 user message 之前的可见历史为快照；
3. 重新执行原问题；
4. 成功后原子替换尾部 user/assistant 对；
5. 后续已有新消息、问题不匹配或公开头变化时拒绝；
6. 失败/取消卡上的按钮是 retry，因为失败轮次没有提交。

Open WebUI 一类消息树能支持任意节点分支，但 MultiRAG 当前 Dialog/Canvas 的公开会话仍是线性数组。
在目标模型、前端和 Channel 都没有原生 branch ID 前，不引入只有 Channel 理解的半套分支 DAG。
未来如支持分支，应新增独立能力与契约，不改变这里的最新轮次语义。

## 11. 实施顺序

| 顺序 | EIM / CHN | 单 PR 目标 | 行为风险 |
|---:|---|---|---|
| 1 | EIM-U11 / CHN-X13 | ✅ 已完成：Provider/Target capabilities、启动预取和目标私有 driver；删除 `ChannelSessionManager`，保持目标执行行为等价 | 低 |
| 2 | EIM-U12 / CHN-U14 | Dialog 改为 detached working copy + 单次 CAS；删除 Dialog 候选写放大 | 中 |
| 3 | EIM-U13 / CHN-U15 | Canvas 候选策略独立化；候选元数据显式化；GC 移出请求热路径 | 中，涉及 DB |
| 4 | EIM-O4 / CHN-O14 | 仅在确认需要重启恢复后增加 durable run ledger、单活约束和 cancel/final CAS | 高，需独立 ADR 复核 |

前三步不改变 Provider runtime 私有 DTO 或执行 SSE wire。X13 使用独立的 additive private preflight：
旧 API 时 worker 降级为无交互 buffered reply，因此不需要 tolerate/emit 双部署。后续若修改
`RuntimeBindingConfig` 或执行 SSE wire，必须停止并按 CHN-ADR-06 重新拆分。

## 12. 验收不变量

- 飞书和钉钉针对同一目标执行相同的目标驱动，不存在 Provider × Target 组合实现；
- Dialog/Canvas 的正常消息与重新生成均满足失败、取消、仅推理不改公开历史；
- 同一公开头上的并发提交最多一个成功，失败方不覆盖后来消息；
- 新会话在首个完整终态前对外不可见，session mapping 只在发布后写入；
- U14 完成后 Dialog 热路径没有候选 insert/delete；U15 完成后 Canvas 热路径没有 TTL prune；
- worker 导入图中没有 SQLAlchemy 和 `api.db`，并继续只走内部 API/SSE；
- Provider capability、Target capability 和实际按钮/错误行为有契约测试；
- 上游同步区相对移植基线不新增 Channel 私有参数或历史分支；
- DB 改动执行 `make verify` + `make integration`，其余改动至少执行 `make verify`。
