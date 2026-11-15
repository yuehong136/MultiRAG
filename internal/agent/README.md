# Go Canvas 的当前能力与接口

Go Canvas 属于 `cmd/`、`internal/` 下的并行实现。当前生产后端与 Web 的
`POST /api/v1/agents/chat/completions` 使用 Python。下面分别说明 Go 的公开接口、
运行时选择配置和可供库调用者使用的组件。

## Agent 数据接口

Go 已有 Agent 列表、模板、提示词、标签、DSL、发布版本及会话数据接口。

| 方法 | 路径 | 当前行为 |
| --- | --- | --- |
| POST | `/api/v1/agents` | 创建调用者拥有的 draft |
| GET/PUT/DELETE | `/api/v1/agents/{agent_id}` | 读取、保存 DSL 或删除 draft |
| POST | `/api/v1/agents/{agent_id}/publish` | 原子追加发布快照 |
| GET | `/api/v1/agents/{agent_id}/versions` | 读取版本列表 |
| GET/DELETE | `/api/v1/agents/{agent_id}/versions/{version_id}` | 读取或删除指定快照 |
| POST/DELETE | `/api/v1/agents/{agent_id}/run` | 验证访问权限后拒绝未实现的执行；取消只处理该 Canvas 已登记的本地流 |

创建要求非空 `title` 和对象形态的 `dsl`，并接受 `description`、`permission`、
`canvas_type`、`canvas_category`。身份从认证上下文读取。更新入口保存 DSL，
并清除 draft 的发布标记。有效 TEAM 成员可按共享权限读取、修改和发布；删除
Canvas 仅限 owner。禁用用户或 tenant、邀请和已撤销成员不能借 TEAM 权限访问。

发布可省略 body 或传 `{}`，也接受 `title`、`description`、`dsl` 覆盖值。
版本与父 Canvas 的修改在同一 SQL 事务内完成，已有快照不会随后续 draft 编辑改变。
会话接口持久化 `API4Conversation` 数据，可创建、读取和删除会话；创建会话不会
执行 Canvas 或生成开场白。

`RunAgent` 当前只检查 Canvas 和可选版本的访问权限，随后返回
`agent execution is not implemented in the frozen target`。它没有接通 Eino 工作流，
也没有公开 run ID、中断恢复、checkpoint、Redis 租约或执行 SSE 协议。
Agent 对话、重跑、聚合标签和 webhook 日志中的未实现路径明确报错；已有运行日志
查询读取实际 Redis 数据，并保留依赖错误。

## 运行时选择配置

`runtime.Selector` 支持 `go`、`python` 和 `auto`。默认值由
`RAGFLOW_CANVAS_DEFAULT_RUNTIME` 决定，未配置时为 Go；tenant 覆盖值保存在
Redis 的 `tenant_canvas_runtime:<tenant_id>`。

`POST /api/v1/admin/canvas-runtime/{tenant_id}` 在实际认证和 SQL 授权后保存选择值。
调用者与 tenant 必须有效，且调用者拥有该 tenant 的有效 owner/admin 关系。
当前 Agent service 尚未使用选择结果分派执行；设置这个值不会切换 Python
生产服务，也不会建立 Go 到 Python 的隐式代理。

## Canvas 与组件库

`canvas.Compile` 可为显式提供组件工厂的库调用者构建 Eino 工作流，返回
`CompiledCanvas.Workflow`。编译选项保留 checkpoint store、state serializer 和
指定节点前后的中断设置；调用者负责实际运行与持久化接线。这些库能力尚未连接到
公开 `RunAgent`。

Canvas 验证确定性 DAG 与显式 Loop，拒绝通用循环和未实现的条件分支语义。
`UserFillUp` 渲染提示并传递已提供的表单值，文件输入明确报错；该组件没有建立
公开的等待用户、interrupt/resume 或文件获取流程。

数值组件保留 `json.Number` 的整数和有限十进制精度。除法的非终止小数保留原有
有限浮点近似；普通 Go 数值使用原有浮点运算。越界、非有限或非零结果下溢报告逐项
`errors`，不写入失败的目标值。列表索引要求精确整数，排序和去重按
实际数值比较。Excel 数字写入仅接受能按 15 个有效位安全往返的值；较大的标识值
需要调用者显式提供字符串，否则拒绝。状态序列化保留嵌套数值与
Sys/Env/Globals/Outputs，但不据此宣称公开接口已提供恢复执行。

LLM 组件的默认 invoker 根据调用者传入的 provider driver、模型名称、API key 和
base URL 构造模型及 Eino adapter。它没有 tenant numeric model ID/default-model
绑定，也不会自动查询 tenant secret store；缺少 driver 或使用 dummy 会报错。
模型 bridge、LLM、Generate 和 Agent 仍受各自未实现能力限制，当前不能宣称实际
供应商的 ReAct 工具调用已经验收。

未实现的 Answer、Retrieval、TavilySearch、ExeSQL、Iteration、IterationItem
组件明确返回错误；工具侧未实现的 AkShare、CodeExec、Jin10、Retrieval、Wencai
同样如此。DocsGenerator 生成实际 DOCX/文本字节；PDF 缺少配置字体时不可用。
产物尚未自动上传到对象存储或加入下载接口。

HTTP 工具与 Invoke/Browser 使用有界响应、超时、公共地址校验和 DNS pinning，
禁止代理与重定向。SQL 工具使用只读事务并拒绝非查询语句。工具凭据需要调用者
显式提供，部署环境共享密钥不会自动成为 tenant 凭据。

OTel provider/handler 可由库调用者显式配置，默认不导出，也不修改全局 provider。
真实 collector 和生产 tracing 尚未验收。

## 验证入口

按 [开发细则](../../docs/development.md#go-并行实现的本地验证) 使用已登记的运行器。
组件与库验证覆盖 `./internal/agent/...`、`./internal/observability/otel`；公开接口
验证覆盖 `./internal/service`、`./internal/handler`、`./internal/router`，包括未实现
执行的明确拒绝。SQL/Redis 专项使用 scratch 数据库与独占 key，并检查独立读回、
事务失败和清理。本机 race、受控 HTTP 与存储合同的结果不替代远程 `ci-required`、
真实供应商或生产验收。
