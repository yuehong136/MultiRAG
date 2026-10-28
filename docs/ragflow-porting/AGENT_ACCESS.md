# Agent REST 权限边界

当前 Agent 的认证身份、资源 owner 和运行用户是不同概念。鉴权使用服务器验证的
Principal；客户端 `user_id` 不授予资源权限。运行时使用 Agent owner 的租户，
发布运行选定已发布版本后保持该快照，继续会话时校验会话执行来源。

## 当前逐操作合同

| 操作 | owner | 有效 TEAM normal/admin | 私有画布外部用户、邀请中或失效成员 |
| --- | --- | --- | --- |
| Agent 详情、版本列表/详情、会话列表/详情/删除、执行日志 | 允许 | 允许 | 拒绝 |
| PUT 元信息、DSL、权限、类别、保存草稿、发布 | 允许 | 拒绝 | 拒绝 |
| DELETE Agent | 允许 | 拒绝 | 拒绝 |
| POST 显式创建会话，含发布模式 | 允许 | 拒绝 | 拒绝 |
| 直接执行已发布 Agent | 允许 | 允许 | 拒绝 |
| 直接执行草稿 | 允许 | 拒绝 | 拒绝 |
| 组件 input-form，读取当前 SQL 草稿 | 允许 | 拒绝 | 拒绝 |
| 组件 debug、reset | 允许 | 允许，保留既有运行访问 | 拒绝 |

TEAM 授权要求 Canvas 的 `permission=team`、当前成员 `status=VALID` 且角色为
normal/admin。owner 按画布真实 `user_id` 判断。版本必须属于路径 Agent；会话
还必须属于该 Agent 且 `source=agent`。执行授权在 SSE 响应开始前完成。

实现锚点：

- [Agent API](../../api/apps/restful_apis/agent_api.py)：`_require_canvas_access`、
  `update_agent`、`set_session`、`input_form`、`debug`、`reset`。
- [Canvas service](../../api/db/services/canvas_service.py)：`require_agent_access`、
  `UserCanvasService.accessible`、`prepare_agent_run`、`get_agent_dsl_with_release`。
- [内部会话管理](../../api/apps/restful_apis/agent_api.py)：`set_session` 对应
  `POST /agents/{id}/sessions`，按服务器 Principal 和 owner 边界创建会话。
- [公开输入读取](../../api/apps/restful_apis/bot_api.py)：`begin_inputs` 使用 tenant 绑定的
  beta token，保留 Agent owner 与发布快照检查。
- [HTTP 权限回归](../../tests/integration/test_agent_access_dependencies.py) 与
  [管理/运行矩阵](../../tests/integration/test_agent_permission_matrix.py)。

## 已确认的本地适配

2026-10-17 用户确认按上表保留现有管理边界。本项已完成逐操作评估；管理操作与团队
运行操作分别授权，不新增协作编辑、授权接口或发布输入表单能力。

| 操作或机制 | 本地处置与依据 |
| --- | --- |
| 集中访问检查与错误信息 | 采用 FastAPI 显式路径参数、Principal 和请求 AsyncSession。共享访问依赖使用通用拒绝信息；owner 管理操作继续使用 owner 拒绝信息，不强求两者文案一致。 |
| 保存、发布和删除 | 保留 owner 约束。PUT 的 owner 行锁、Canvas/版本原子提交和 Redis replica 归属均不变。更新字段没有 `user_id`，不把编辑者改写成资源 owner。 |
| 显式创建会话 | REST Explore 与 SDK 均保留 owner 约束，包括发布模式；不把直接运行已发布 Agent 的团队权限扩展到这个管理入口。 |
| 组件 input-form | 保留 owner 约束，继续读取当前 SQL 草稿；不改成团队访问，也不增加新的快照选择合同。 |
| debug、reset | 保留既有团队运行访问及实现。reset 会写回 SQL DSL，因此这里的“管理边界”是上表列明的操作合同，不等于禁止团队触发任何草稿状态写入。 |
| 运行与共享 DSL helper | 保留开流前授权、异步 DB/阻塞桥接和不可变发布来源。`get_agent_dsl_with_release` 的 owner 检查继续保护 SDK 创建会话和 beta token 输入读取，不删除该共享保护。 |
| 回归与 Webhook | 使用真实路由依赖和 HTTP/SQL/Redis 读回验证权限，不以调用装饰器的假函数替代端点验收。Webhook 触发认证与日志合同保持现状，无需因 CRUD 权限评估调整其生产实现。 |

独立 Web 的 `src/api/agent.ts:setAgent` 将保存、元信息和发布交给同一 PUT；
`use-save-graph.ts` 的保存、自动保存、普通编辑器与嵌入编辑器发布都使用此调用。
保持该 PUT 的 owner 约束，避免同时开放 DSL、权限和发布操作。`SingleStepDebugSheet`
继续消费 owner-only 的 input-form；既有团队 debug/reset 访问保持不变。

Explore 的 `use-explore-run-request.ts` 在首次运行前显式创建会话；这个路径仍限 owner。
团队成员直接运行已发布 Agent 的已有能力继续保留，不据此宣称团队 Explore 已开放。
上述消费者只作源码核对，本项没有 Web 代码改动或新增页面能力。

现有 HTTP 回归覆盖有效 normal/admin、邀请/失效/非成员、草稿与多个已发布版本、旧会话
快照、错误 Agent/session/version、伪造客户端身份及拒绝后的 SQL/Redis 无写入，并验证
owner-only 的更新、输入表单、删除及显式创建会话。没有因本次结论变化重跑源码未变的
全套门禁，也不以这些后端证据替代 Web 页面验收；统一合入后的组合回归另行记录。
