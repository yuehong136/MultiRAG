# Go Canvas 的当前能力与接口

Go Canvas 属于 `cmd/`、`internal/` 下的并行实现。生产后端和当前 Web 的
`POST /api/v1/agents/chat/completions` 仍使用 Python；这里没有运行时选择器或隐式代理切换。

## 执行入口

Go 服务在既有 Agent 读取、标签、版本、会话和上传接口之外提供：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/v1/agents` | 创建调用者拥有的 draft |
| GET/PUT/DELETE | `/api/v1/agents/{agent_id}` | 读取、保存或删除 draft |
| POST | `/api/v1/agents/{agent_id}/publish` | 原子追加当前 draft 的发布快照 |
| DELETE | `/api/v1/agents/{agent_id}/versions/{version_id}` | 删除该 Agent 的指定快照 |
| POST/DELETE | `/api/v1/agents/{agent_id}/run` | 执行、恢复或请求取消指定 run |

创建要求 `title`、对象形态的 `dsl`；创建和更新接受 `description`、`permission`
（`me` 或 `team`）、`canvas_type`。调用者身份与 ID 不从请求体读取。
修改、发布、删除均仅限 owner；有效 TEAM 成员可以读取和执行共享 Canvas。
禁用 owner/tenant、邀请或已撤销成员不能借 TEAM 权限访问。

发布请求可以省略 body 或传 `{}`，只发布已保存的 draft。版本、父 Canvas
更新和级联删除在同一 SQL 事务内完成；保存新 DSL 会清除 draft 的发布标记。
发布快照不随后续 draft 编辑覆盖。此入口不等同于 Python 的自动保存版本策略。

首次执行示例：

```json
{"query":"hello","inputs":{},"version_id":"optional-version-id"}
```

`version_id` 省略时执行当前 draft，提供时必须属于路径中的 Agent。
执行接受有界的 v1 `components` DSL，保持节点 ID 和数值模型 ID；不做有损 v2 转换。`upstream` 与 `downstream` 必须互认，未知、重复及不一致的边会拒绝。
`user_id`、`state` 和内部字段不能通过 `inputs` 覆盖身份或引擎状态。
会话 ID、`release`、OpenAI 兼容选项和 query-string 运行选项不属于此接口。

执行返回 SSE，每帧为 `data: <JSON>\n\n`，包含 `event`、`run_id` 和可选 `data`：

- `workflow_started`：实际运行开始。
- `message`：终端 Message 节点的真实文本，含 `content`、`component_id`。
- `waiting_for_user`：真实 Eino 中断，含 `interrupt_id`、`input_spec`。
- `workflow_finished`、`error` 或 `cancelled`：本次执行的终态。

恢复请求必须使用等待事件返回的 ID：

```json
{"run_id":"32-hex-id","interrupt_id":"returned-interrupt-id","resume":{"answer":"value"}}
```

普通恢复值作为 opaque `user_input` 传给后续节点；没有完整的字段级表单验证。声明文件类型会在 Native 编译阶段拒绝。

恢复仅接受这些字段。认证 actor、路径 Agent 和中断 ID 必须同时匹配；不能用共享
Canvas 的权限接管另一 actor 的 run。恢复使用首次执行保存的 DSL 和输入，不重读新 draft。
取消请求为 `DELETE .../run`，body 为 `{"run_id":"32-hex-id"}`。

## 状态、取消与依赖

Redis 是公开执行的必需依赖。运行元数据、中断快照和取消标记的 TTL 为 24 小时；
每次执行限制为 5 分钟，Redis 6 分钟租约排除并发恢复。暂停不保持后台执行 goroutine。
完成、失败及暂停后的取消会删除 checkpoint；运行元数据保留至 TTL，便于区分终态。取消登记、最终状态、checkpoint 删除和租约释放采用原子 Redis 脚本协调，避免暂停收尾的取消竞态。
请求断开、超时及取消都会向模型与工具传递 context。

运行状态快照深拷贝嵌套值，checkpoint 保留大整数和 Sys/Env/Globals/Outputs。
UserFillUp 通过 Eino 原生 interrupt/resume 恢复，已完成的前驱不会重跑。

模型解析始终使用 Canvas owner 的 tenant，接受现有 numeric tenant model ID 或该 tenant
的默认聊天模型。DSL 中的 driver、API key、base URL 不作为凭据来源。
模型 bridge 使用保存的授权配置；当前非空工具调用和多模态能力会明确拒绝，不能据此
宣称实际供应商已通过 ReAct 工具调用验收。单独工具的受控协议测试也不代表供应商可用性。

## 明确的能力边界

引擎支持确定性 DAG、显式 Loop 和顶层 UserFillUp。通用循环、Switch/Categorize 控制流节点、
Loop 内 UserFillUp 以及多个同时中断当前会拒绝，防止漏执行或执行错误分支。

未实现的 Answer、Retrieval、TavilySearch、ExeSQL 组件和 Iteration 组件不会注册成空成功。
工具侧未实现的 AkShare、CodeExec、Jin10、Retrieval、Wencai 同样会明确报错。
LLM、Generate、Agent 的调用还受模型 bridge 的实际能力限制。

DocsGenerator 的 DOCX/文本输出是实际字节，不返回伪下载 URI。PDF 缺少已配置字体时
明确不可用；产物尚未自动上传到对象存储或加入现有下载接口。文件表单、会话创建和
对话持久化、记忆保存、引用生成、Webhook/日志查询没有通过这个执行入口交付。
已有会话读取、规范化及删除的独立合同保持不变。

HTTP 工具与 Invoke/Browser 使用有界响应、超时、公共地址校验和 DNS pinning，
禁止代理与重定向。SQL 工具使用只读事务，保留字面量并拒绝非查询语句。
工具凭据没有自动从 tenant secret store 注入；不能将部署环境的共享密钥当作 tenant 凭据。

OTel provider/handler 是可显式配置的库，默认不导出，不修改全局 provider；
尚未建立真实 collector 和生产 tracing 验收。接线和验证需要单独的明确配置。

## 验证入口

按 [开发细则](../../docs/development.md#go-并行实现的本地验证) 使用已登记的运行器。
核心定向包为 `./internal/agent/...`、`./internal/observability/otel`，公共链验证覆盖
`./internal/service`、`./internal/handler`、`./internal/router`。
SQL/Redis 专项使用全新的 scratch 数据库与随机独占 key，检查独立读回、事务失败和清理。
本机 race、受控 HTTP 和存储合同通过不替代远程 `ci-required`、真实供应商或生产验收。
