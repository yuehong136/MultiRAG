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
| 组件 debug、reset | 允许 | 当前允许，见下文待决项 | 拒绝 |

TEAM 授权要求 Canvas 的 `permission=team`、当前成员 `status=VALID` 且角色为
normal/admin。owner 按画布真实 `user_id` 判断。版本必须属于路径 Agent；会话
还必须属于该 Agent 且 `source=agent`。执行授权在 SSE 响应开始前完成。

实现锚点：

- [Agent API](../../api/apps/restful_apis/agent_api.py)：`_require_canvas_access`、
  `update_agent`、`set_session`、`input_form`、`debug`、`reset`。
- [Canvas service](../../api/db/services/canvas_service.py)：`require_agent_access`、
  `UserCanvasService.accessible`、`prepare_agent_run`、`get_agent_dsl_with_release`。
- [SDK session](../../api/apps/sdk/session.py)：`create_agent_session`、`begin_inputs`。
- [HTTP 权限回归](../../tests/integration/test_agent_access_dependencies.py) 与
  [管理/运行矩阵](../../tests/integration/test_agent_permission_matrix.py)。

## 协作能力的待决适配

以下为待确认方案，不代表已授权或已实现的权限调整。

独立 Web 的 `src/api/agent.ts:setAgent` 将保存、元信息和发布交给同一 PUT。
`use-save-graph.ts` 的保存、自动保存、普通编辑器与嵌入编辑器发布都使用此调用。
直接允许团队成员访问 PUT 会同时授予修改 DSL、权限及发布能力；应先决定操作边界。
如允许团队编辑草稿，建议仅放行 DSL/title/description/avatar，权限、类别和 release
继续由 owner 管理，并保持 SQL owner、事务、版本及 Redis replica 的租户归属。

组件输入表单由 Web `SingleStepDebugSheet` 消费，当前读取 SQL 草稿；详情接口已经
允许团队读取该草稿，而 debug/reset 也允许团队访问。尤其 reset 会写回 SQL DSL。
这些接口与 owner 管理草稿的目标存在逐操作差异，须一起确定团队可读、可调试与可写
的范围，不能只放开 input-form 后声称协作权限已经完整。

Explore 的 `use-explore-run-request.ts` 在首次运行前显式创建会话，因此后端直接发布
运行支持团队成员，不等于 Explore 的该路径已经可用。如开放团队发布会话，建议只允许
明确的发布模式，从实际已发布 revision 生成不可变执行来源；后续输入表单和继续运行
都绑定该 revision，不能改读最新草稿。组件输入表单的新发布模式也应使用该选定快照。

SDK 创建会话和 beta token 输入读取仍依赖 `get_agent_dsl_with_release` 的 owner 检查。
替换共享 helper 的校验前，必须分别证明各入口已经认证且拥有目标 Agent 的访问权。

适配验收需覆盖有效 normal/admin、邀请/失效/非成员、草稿与多个已发布版本、旧会话
快照、错误 Agent/session/version、伪造客户端身份及拒绝后的 SQL/Redis 无写入；涉及
Web 时还需实际保存、发布、创建会话和独立读回，合同测试不能替代页面运行验收。
