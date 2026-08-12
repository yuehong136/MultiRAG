# MultiRAG Run Platform

> 状态：**F0 已完成；RUN-F1 机器契约/纯状态机垂直切片进行中；目标 v2 路由与持久化尚未实现**
> 最后核验：2026-08-13
> 维护范围：远程 Run API、持久化状态机、事件协议、事务 outbox、Valkey Streams 投递、取消授权与发布顺序

本目录是 MultiRAG **远程 Run Platform** 的权威文档入口。它定义 Web、未来桌面客户端、Channel
和其他受信调用面如何创建、观察、恢复和取消一次长期运行。它不替代 Dialog/Canvas 的业务执行
实现，也不复制 Channel Provider、企业身份或 MCP 授权文档。

## 1. 先区分当前 v1 与目标 v2

### 当前 v1：request-bound SSE

当前已上线的 Channel 执行路径是：

```text
POST /api/v1/internal/channel-bindings/{binding_id}/executions
  -> 当前 API 请求内启动目标执行
  -> 同一个 HTTP 响应直接返回 message_delta / message_completed / execution_failed
  -> data:[DONE] 结束
```

其事实边界是：

- `api/apps/restful_apis/channel_execution_api.py` 在一次 `POST` 生命周期内创建并消费执行流；
- `api/channels/runtime_client.py` 在同一连接内读取并校验 SSE；
- Channel 的 `queued/running/final/error/cancelled` 主要是 worker 进程内回复状态；
- CHN-U16 已覆盖可协作停机，但不承诺 `kill -9`、机器故障、跨实例接管或断线后事件重放；
- 当前没有通用 `run` / `run_event` / `run_outbox` 持久化实体，没有可查询的远程 Run API，
  也没有以 PostgreSQL 为真相源的 `Last-Event-ID` 续传。

这条 v1 路径继续是现状契约，不能把本目录的目标设计描述成已上线能力。

### 目标 v2：durable Run Platform

目标 v2 把“请求连接”和“运行生命周期”分离：

```text
Create Run -> PostgreSQL 原子持久化 -> 异步执行 -> 持久化事件/outbox
                                               -> Valkey Streams 投递
Get/WS/SSE Events <- PostgreSQL 回放 + Valkey 低延迟通知
Cancel Run -> 鉴权/授权 -> 持久化 cancel intent -> CAS 竞争终态
```

客户端收到 `202 Accepted + run_id` 后，即使断开、重启或切换设备，也能查询 Run、按序回放事件，
并在授权成立时请求取消。PostgreSQL 是状态和事件的唯一真相源；Valkey Streams 只负责低延迟投递，
不能决定 Run 的最终状态。

实时首选 `WS /api/v2/runs/stream` 多 Run 订阅；企业代理阻断 WebSocket 时按同一 cursor 降级到单 Run
SSE 回放。慢消费者必须收到 `slow_consumer + cursor` 后主动重连，不能让服务端内存无界增长。

## 2. 文档索引与权威边界

| 文档 | 唯一回答的问题 |
|---|---|
| [ARCHITECTURE](ARCHITECTURE.md) | 当前 v1 与目标 v2 的组件、实体、状态机、事务和故障边界 |
| [CONTRACT](CONTRACT.md) | v2 API、事件 envelope、游标/续传、幂等、取消授权和兼容规则 |
| [ROADMAP](ROADMAP.md) | 阶段、依赖、工作量、验收、部署顺序和 v1/v2 灰度 |
| [DECISIONS](DECISIONS.md) | `RUN-ADR-NN` 决策及其替代方案、后果和失效条件 |

相邻项目只维护链接，不复制本目录内容：

- [Channel 项目](../channel-program/README.md)：Provider、ReplySession、Channel worker、卡片交付和
  Channel 专属兼容路径；
- [Channel 执行架构](../channel-program/EXECUTION_ARCHITECTURE.md)：当前 Provider × Target 边界、
  Dialog/Canvas 历史事务与 CHN-O14 的原始需求背景；
- [企业身份与 MCP](../enterprise-identity-mcp/README.md)：Principal、AuthenticationContext、
  Provider 身份解析、委托 token、PDP 和 MCP resource authorization；
- [MCP 契约](../enterprise-identity-mcp/CONTRACTS.md)：工具授权、确认、重放保护和未知副作用语义。

## 3. 本目录拥有与不拥有的内容

### 本目录独占

- 远程 Run API 的路径、资源模型、幂等和错误语义；
- `Run`、`RunEvent`、`RunOutbox` 的目标实体和 PostgreSQL 状态机；
- Run 事件 envelope、每 Run 序列、四种终态与回放不变量；
- PostgreSQL outbox 到 Valkey Streams 的发布、重复投递和恢复边界；
- 创建、读取、WS/SSE 订阅、interaction、取消 Run 的认证与授权依赖；
- v1/v2 共存、灰度、回滚和跨服务部署顺序。

### 本目录不拥有

- Dialog/Canvas 内部 checkpoint、prompt、模型选择和业务历史结构；
- Feishu/DingTalk 的 webhook、卡片、队列和交付实现；
- Principal 的构造、外部身份解析、MCP token 与具体业务数据授权；
- Web/桌面 renderer 的状态管理、渲染器和本地 Agent Host 协议；
- 具体依赖版本清单。本项目只要求使用仓库已批准的受支持稳定通道，版本升级另行核验，
  不在多个文档维护重复数字。

## 4. 不可破坏的不变量

1. **先持久化，后执行。** API 返回已接受前，Run 与首事件必须在 PostgreSQL 原子提交。
2. **PostgreSQL 是唯一真相源。** Valkey 丢键、trim、重启或重复消息不能改变 Run 状态。
3. **事件只追加。** 同一 Run 的 `seq` 严格递增；已提交事件不原地改写。
4. **终态唯一且不可逆。** `completed`、`failed`、`cancelled`、`interrupted` 只能竞争成功一个；
   retry/regenerate 创建新的 `run_id`。
5. **取消是受权意图，不是连接断开。** 断开 SSE 不等于取消；取消先鉴权、再持久化、再协作终止。
6. **Run 与公开历史分离。** delta、tool trace、失败和取消不冒充 Dialog/Canvas 已完成消息。
7. **没有双执行 fallback。** 同一业务请求只能由 v1 或 v2 中一个执行所有者接管；v2 已接受后不得
   因超时自动改走 v1。
8. **服务端解析租户与主体。** 请求体不能覆盖 `tenant_id`、`principal_id`、授权结果或目标发布版本。
9. **高频输出先合并。** 不按模型 token 写 PostgreSQL；持久化的是有界合并后的增量与权威终态。
10. **跨版本先 tolerate 后 emit。** 新字段/事件先让旧消费者安全忽略或拒绝，再由生产者发出。

## 5. 当前交付边界

F0 已建立文档基线。RUN-F1 当前只新增无副作用的协议核心：

- `api/run_platform/domain.py`：九状态、四终态和完整合法转换表；
- `api/run_platform/schemas.py`：九字段 v2 envelope 与 `message.delta` data 的 strict schema；
- `api/run_platform/events.py`：按 `seq` 的纯投影、identity 稳定、terminal 不可追加；
- `api/run_platform/protocol/generated/`：两份由 Pydantic 真源生成并可 `--check` 的 JSON Schema。

仍未实现且不得宣称具备：

- `/api/v2/runs` 路由、PostgreSQL Run/Event/Outbox 表、dispatcher、worker claim、WS/SSE gateway；
- EIM/Channel/SDK 的 Principal 或 workload 映射、团队角色策略、OIDC provider-subject 绑定；
- 不改变当前 Channel v1 SSE wire；
- 不启动 CHN-O14 的接入，也不宣称已有 durable recovery；
- 后续任何实现阶段必须在 [ROADMAP](ROADMAP.md) 记账，并在改变上述不变量前新增或修订
  [DECISIONS](DECISIONS.md) 中的 ADR。

## 6. 冷启动阅读顺序

1. 本文件的当前/目标边界；
2. [ARCHITECTURE § 当前 v1](ARCHITECTURE.md#2-当前-v1request-bound-sse-事实基线)；
3. [CONTRACT § 事件不变量](CONTRACT.md#6-事件-envelope-与不变量)；
4. [ROADMAP § 部署与灰度](ROADMAP.md#6-部署顺序与-v1v2-灰度)；
5. 若任务涉及 Channel，再读 Channel 项目；若涉及主体、授权或 MCP，再读 EIM 项目。
