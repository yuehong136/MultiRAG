---
sidebar_position: 2
slug: /http_api_reference
---

# HTTP API 参考

本文档提供 MultiRAG HTTP API 的完整参考。

## 基本信息

### 基础 URL

```
http://<your-server>:8123/api/v1
```

### 认证

所有 API 请求都需要在 `Authorization` 头中提供 API 密钥：

```
Authorization: Bearer <your-api-key>
```

### 请求格式

- Content-Type: `application/json`
- 请求体使用 JSON 格式

### 响应格式

所有响应均为 JSON 格式。成功响应结构：

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

错误响应结构：

```json
{
  "code": 1001,
  "message": "Error description"
}
```

## Task API

### 提交取消请求

接受当前 Web 会话 JWT 或 API Key（`Authorization: Bearer ...`）。
两种请求执行相同的授权与取消逻辑：

| 方法与完整路径 | 请求体 |
|---|---|
| `POST /api/v1/tasks/{task_id}/cancel` | 无需请求体 |
| `PATCH /api/v1/tasks/{task_id}` | `{"action":"stop"}`，不接受额外字段 |

`task_id` 为 1–32 个字母、数字、下划线或连字符。没有公开的 GET Task 接口。

| 运行类型 | 可取消的 ID 与授权依据 |
|---|---|
| 文档解析 | SQL Task ID；有效文档、知识库与当前已加入的 owner/normal/admin 成员 |
| GraphRAG、RAPTOR、MindMap | 启动结果的 `task_id`；由有效知识库登记的任务字段反查归属 |
| Agent | 运行 SSE 的 `task_id`，与 `message_id` 不同；服务器在首帧前登记本次尝试与画布归属 |
| DataFlow 调试 | 运行响应 `data.message_id` 就是 Task ID；服务器在真实入队前登记本次尝试与画布归属 |

Agent/DataFlow 的 owner 可取消；其他当前已加入成员还需要画布为 team。
请求中的 `user_id`、`tenant_id`、DSL 与仅持有 UUID 均不构成授权。
取消按单次尝试隔离，不取消同一画布的其他并发运行，也不改草稿或发布版本。

Python 返回既有 `retcode/retmsg` 结构；Go 返回 `code/message`，业务码相同：

```json
{"retcode":0,"retmsg":"success","data":true}
```

- `0`：已提交取消请求，或已结束/已取消/不存在的任务无需变更。成功不证明 worker 已停止。
- `109`、`data:false`：已知任务无权限、资源无效，或 DataFlow 调试任务没有可信归属。
- `100`、`data:false`：SQL、Redis 或补偿失败，不能当作取消成功。
- Python 缺失/无效认证为 HTTP 401；ID、PATCH action 或额外字段错误为 HTTP 422。
  Go 使用现有鉴权及参数错误响应，调用方同时检查 HTTP 状态和 `code`。

文档 Task 变为 `progress=-1` 并追加一次取消记录；正在运行/排队的文档变为 CANCEL、
`progress=0`。图谱和 DataFlow 哨兵不作为文档 ID 更新。
迟到的 Task 进度与文档启动写入不能覆盖此次取消，普通失败任务仍可重试。
Redis 与 SQL 不构成分布式事务；提交失败尝试仅撤销本次请求的 Redis nonce，
补偿不能确认时仍返回失败，需要重新检查任务状态。

服务器运行归属登记与取消标志有效期为 24 小时，运行结束保留有限期终态；
取消通过 CAS 与结束竞争，重复取消不重复写日志。旧 Agent ID 无登记或登记已过期时
无副作用成功；仍存在但没有可信登记的旧 DataFlow 调试任务返回 `109`。
当前没有额外任务查询 API，可从运行流的错误/终态和已有解析进度确认运行结果。

Agent 在成功助手消息写入前，原子判定结束与取消的胜负。取消获胜时记录本次用户输入与
错误，轮数只增加一次，不追加本次成功助手，也不发送成功 `workflow_finished`、
`message_end` 或 `[DONE]`。结束获胜后到达的取消是幂等操作。运行登记的 finished 表示
不再接收取消，后续消息写入或传输仍可能失败；不能据此推断答案已交付。
取消、执行或收尾失败的会话保留已有成功历史及本轮用户输入；本轮助手答案不进入
SQL DSL 的 `history`、`globals.sys.history` 或下次运行的 `get_history` 模型输入。
不会为失败清空此前历史、改写 Agent 定义/发布版本或编辑器副本。
流式取消前已发送的内容片段无法收回。客户端在组件等待或帧发送处断连时，响应负责
关闭整个执行迭代链，记录尚未提交的失败轮次；关闭期间不发送错误帧，关闭与取消仍向外传播。
已经提交的轮次不重复改写，断连后不保证错误帧送达。无 SQL 会话的编辑器调试运行
从可信运行登记起负责收尾，包括响应头尚未发送、执行迭代尚未开始时的发送失败或取消。
此时不启动 Canvas；响应关闭回调与执行 finally 共用一次收尾，失败不保存副本，
原取消胜方及 nonce/TTL 保持不变；已提交的成功副本保持原值。
API 取消 nonce 保留 24 小时；无 API nonce 时运行清理的内部标记 `x` 保留 1 小时，
清理采用 SET NX，不替换 API nonce 或缩短其有效期。

旧 `PUT /v1/canvas/cancel/{task_id}` 已退役，返回 HTTP 404，OpenAPI 不再列出此路径。
Web 的 Agent/DataFlow 取消均已迁移到上述 POST；PATCH `action: "stop"` 合同继续保留。
旧路径不执行取消服务，不修改任务、文档、运行登记或取消标志。

## Agent API

### 更新与发布画布

**PUT** `/agents/{agent_id}`（完整路径 `/api/v1/agents/{agent_id}`）。
接受 owner 的 web 会话 JWT 或 API Key，其他用户不能更新。
请求采用增量更新，`null` 字段忽略；`dsl` 可以是 JSON 对象或对象的 JSON 字符串。

| 请求 | 行为 |
|---|---|
| `dsl` 加 `release: true` | 保存当前画布并发布对应版本 |
| `dsl` 加 `release: false`，或省略/置空 `release` | 保存草稿，保留此前的发布快照 |
| 只有 `release: true/false` | 发布当前 DSL 或保存当前 DSL 的草稿版本 |
| 只有标题、描述、头像、权限等元数据，省略/置空 `release` | 更新元数据，保留当前发布标志，不改版本历史 |
| 空对象或只有 `null` 字段 | 无变更 |

`release` 推荐传布尔值。兼容字符串 `true`/`false`/`1`/`0`/空字符串，
忽略大小写和两侧空白；`false`/`0`/空字符串映射为 `false`，不会因字符串非空而发布。
其他字符串、数字、数组或对象返回 HTTP 422，JSON `detail` 指明 `body.release`。

此组既有 Agent 路由使用 `retcode/retmsg` 响应。更新成功为 HTTP 200：

```json
{"retcode": 0, "retmsg": "success", "data": true}
```

业务失败检查非零 `retcode`；缺失/无效认证为 HTTP 401、`code=401`。
版本写入、清理和 Canvas 更新处于同一数据库事务，任一步失败均回滚。
数据库提交后的 Redis 副本同步失败返回非零 `retcode` 与
`agent saved, but replica sync failed.`；数据库内容已保存，可重试同一请求同步副本。
数据库与 Redis 不构成分布式事务。

**GET** `/agents/{agent_id}` 的 `data.release` 表示当前画布的发布状态；
`last_publish_time` 表示已有发布版本的最近更新时间。保存草稿后，前者变为 `false`，
后者仍可存在。`/agents/{agent_id}/versions` 中每个版本的 `release` 独立表示该快照是否发布。
相同 DSL 重复保存复用最新版本；发布快照后的草稿保存另建版本，发布快照始终保留，
未发布版本只保留最新 20 个。

创建 `/agents/{agent_id}/sessions` 或首次调用 `/agents/chat/completion` 时传
`release: true`，使用最新发布快照，即使当前画布已有新草稿。已有会话继续使用创建时保存的 DSL。

### 运行发布版本的授权与错误

普通 **POST** `/agents/chat/completion` 首次传 `release: true`，或传已有 `session_id` 时，
先验证当前认证身份能访问该 Agent，再固定本次要运行的 DSL。
owner 可以运行；其他用户需要已加入 owner 的团队（成员关系 `status=1`，角色为
`normal` 或 `admin`），且画布 `permission` 为 `team`。
未接受邀请的 `invite` 角色和失效成员关系即使已有记录也不能运行，首次运行与已有会话均返回 403。
私有画布即使有团队 membership 也不能由其他用户运行。
更新/发布画布及显式 **POST** `/agents/{agent_id}/sessions` 仍只允许 owner。

准备阶段的失败在 SSE 响应头发送前返回 JSON，流式与非流式请求行为相同：

| 情况 | HTTP 状态 | 响应 |
|---|---|---|
| 缺失、无效或失效认证身份 | 401 | `code=401`，`message` |
| Agent 或会话不存在 | 404 | `retcode=102`，`retmsg`，`data=false` |
| 无运行权限 | 403 | `retcode=103`，`retmsg`，`data=false` |
| 首次发布运行找不到发布版本 | 409 | `retcode=102`，`retmsg=No available published version`，`data=false` |
| 其他准备失败 | 500 | `retcode=100`，`retmsg`，`data=false` |

调用方同时检查 HTTP 状态与业务码；此处是普通 Agent 运行路径的合同。
选中的发布 DSL 与版本标题由同一条快照记录取得，运行直接消费这些准备结果。
即使准备后出现新发布版本，本次也继续运行选中的快照；已有会话继续使用自己的 DSL。

运行中的错误在非流式请求中返回非零 `retcode`，不返回成功答案。
流式请求已经开始时，返回 `event=error`、`code=100`、`message` 及 `data.error`，
随后结束传输，不发送 `[DONE]`；成功流式完成才发送 `data:[DONE]`。
未处理的组件错误、显式 error 事件和运行异常不会被事件过滤吞掉。
失败会话记录错误和本次用户输入，不追加本次的成功助手答案；已有历史答案保留。
成功的 `message_end` 在运行结果持久化后才发送。

### 会话变量默认值与重置

Agent DSL 的 `variables` 保存定义，例如
`{"items": {"type": "array<string>", "value": ["seed"]}}`；
`globals["env.items"]` 保存本次运行值，组件通过 `{env.items}` 引用。
重置只恢复运行值，不改写 `variables.items.value`。`value` 非 `null` 时直接采用，
包括 `false`、`0`、`""`、`{}`、`[]`；object/list 在运行前深拷贝，append 不会污染默认值。
缺少 `value` 或为 `null` 时，number/boolean/object/array 分别恢复为
`0`/`false`/`{}`/`[]`，string、未知或缺少类型恢复为 `""`。
定义已存在但 `globals` 尚无对应键时也会初始化；序列化会保存这些运行值。

历史无变量 DSL 可省略 `variables`，或保留 `{}`、`[]`。其中空列表仅在 Canvas
运行视图中按空映射处理，保存的 DSL 仍为 `variables: []`，无需改写模板或已存会话。
reset 将无定义的 `env.*` 运行键恢复为 `""`；已有会话继续运行时保留这些运行值。
有变量定义时仍须使用名称到定义的映射；非空列表、`null`、字符串和数字不在此兼容范围内，
reset 不会将它们静默转换为空变量。

| 操作 | 变量与会话状态 |
|---|---|
| **POST** `/agents/{agent_id}/sessions`，body `{"release": false}` 或 `{"release": true}` | 从草稿或最新发布快照创建新会话，恢复默认值、清空 history/path；成功 `retcode=0`，新 ID 为 `data.id` |
| 普通 **POST** `/agents/chat/completion`，首次传 `release: true` | 首次运行发布快照并创建会话，恢复默认值；`stream` 可为 true/false |
| 同一路径传 `agent_id`、`session_id`、`query`、`stream` | 装载该会话保存的 DSL，继续运行值和历史；`release` 不替换已有会话 DSL |
| **POST** `/agents/{agent_id}/reset`，空 body | SQL Canvas 恢复变量默认值并清空运行状态，成功返回 `retcode=0` 和 `data` DSL；既有会话、版本和 Redis 编辑器副本保持原状态 |
| **POST** `/agents/{agent_id}/components/{component_id}/debug`，body `{"params": {}}` | 从 SQL Canvas 建立临时运行实例并 reset；返回组件输出，不保存 Canvas 或会话状态 |

普通会话运行的 SSE 成功帧带本次 `session_id`，非流式成功响应为
`{"retcode": 0, "data": {"data": {"content": "..."}, "session_id": "..."}}`。
前端应将 Explore 当前选择的会话 ID 放入运行请求；切换选择后仍把在途结果归属到
发起请求的会话，禁止用该流返回的 ID 覆盖另一个已选会话。新建会话后使用 `data.id`。
运行失败沿用上一节的 HTTP/业务码及 SSE error 语义。

无 `session_id` 且 `release` 非 true 的普通运行使用 Redis 编辑器副本，不创建 SQL 会话，
不能当作上述新会话重置操作。需要草稿新会话时先显式创建，再传该 `session_id` 运行。
`openai-compatible: true` 的消息适配路径另用 OpenAI 响应形状，不能套用普通 SSE 会话 ID 合同。

### 列表操作组件与历史 DSL

`ListOperations` 的参数位于 `components[component_id].obj.params`。
新建节点须显式保存整数 `operations_version: 2`，例如：

```json
{"query": "{begin@items}", "operations_version": 2, "operations": "nth", "n": -1, "strict": false}
```

| v2 操作 | `strict: false`（默认） | `strict: true` |
|---|---|---|
| `nth` | 正数按 1 起算，负数从末尾起算，返回单项数组；0 或越界返回 `[]` | 要求 `n != 0` 且 `abs(n) <= 列表长度`，否则报错 |
| `head` | 返回前 N 项；N < 1 返回 `[]`，N 超过长度返回全部 | 要求 `1 <= N <= 列表长度`，否则报错 |
| `tail` | 返回后 N 项，保持原顺序；N < 1 返回 `[]`，N 超过长度返回全部 | 要求 `1 <= N <= 列表长度`，否则报错 |

v2 省略或清空 `operations` 时默认 `nth`；旧名 `topN`（忽略大小写和两侧空白）
作为 `head` 的别名。`n` 默认 0，按 Python `int` 转换：整数、整数字符串、有限小数
（向 0 截断）和布尔值兼容；无法转换时按 0 处理，再执行上述范围规则。
`strict` 推荐传布尔值；字符串 `true/1/yes/on` 为真，忽略大小写与两侧空白，
其他字符串（包括 `false/0/no/off`）为假。

`operations_version` 缺失或为整数 1 时，始终保留历史语义：

| 历史操作 | 执行结果 | 显式转换为 v2 时的等价规则 |
|---|---|---|
| `topN` 或省略 `operations` | 前 N 项；N < 1 返回 `[]`，超长返回全部 | 改为 `head`，保留 n，`strict: false` |
| `head` | 第 N 项的单项数组；非正数或越界返回 `[]` | 改为 `nth`；转换后的 N > 0 时保留，否则置 0；`strict: false` |
| `tail` | 倒数第 N 项的单项数组；非正数或越界返回 `[]` | 改为 `nth`；转换后的 N > 0 时取 -N，否则置 0；`strict: false` |

历史参数中的冗余 `strict` 不改变执行。转换 n 时采用上面的 `int` 规则；
不能把历史负数 `head/tail` 直接改成负数 `nth`，否则会把原空结果改为有效单项。
保持版本 1 即可继续编辑和运行历史节点，无需转换或批量改写存储。
其他版本值（包括字符串 `"2"` 和布尔值）被拒绝，不能依据操作名或 strict 字段猜版本。

编辑器加载旧节点时应先确定版本 1，再合并默认字段，避免新建默认版本 2 覆盖历史语义。
导入、复制、保存和发布保留该标记。运行只在 DSL 副本中补齐历史版本 1，
会话序列化保留标记；运行及组件 debug 不改写原草稿、发布快照或 Redis 编辑器副本。
显式 reset 会保存重置后的 SQL Canvas，不改已有会话、版本和 Redis 副本。

输入值为 `null` 时按空列表处理，其他非列表值报 `TypeError`；strict 越界报 `ValueError`。
`filter/sort/drop_duplicates` 保持原行为。输出字段仍为 `result/first/last`，
空结果的 first/last 为 `null`。失败清空结果，不复用上一轮输出。
普通会话运行的错误沿用上一节非零业务码和 SSE error 合同；
普通发布运行在构造 Canvas 时发现无效版本，开流前返回 HTTP 500、`retcode=100`，不创建会话。
OpenAI 消息适配的 strict 失败返回 `{"error":{"message":"...","type":"server_error","code":100}}`；
流式失败发送同形状的 SSE 后结束，不发送成功 choices 或 `[DONE]`，非流式失败也不返回 choices。
失败会话保存本次用户输入及错误，不追加成功助手答案。

### 分享与嵌入 Agent 的补全

**POST** `/agentbots/{agent_id}/completions`（完整路径 `/api/v1/agentbots/{agent_id}/completions`）。
使用 `Authorization: Bearer <beta token>`；普通 API Key 或 web JWT 不能替代 beta token。
请求包含 `query`（也兼容 `question`）、`inputs`、`files`、`session_id`、`stream` 和 `release`。
省略 `stream` 时默认 true。body 的 release 缺失/为 null 时才采用同名 query 参数；
首次 `release: true` 运行发布快照，已有会话继续自己的 DSL。

`stream: false` 消费完整执行，检查每一帧错误并聚合 message 内容和引用。
成功使用 SDK 的 `code` 外层，data 为终态对象，不是 started 帧的 SSE 字符串：

```json
{"code": 0, "data": {"event": "message_end", "session_id": "session-id", "data": {"content": "[\"a\", \"b\"]", "reference": {}}}}
```

没有 Message 的已完成工作流可以返回 `event=workflow_finished`，保留 `data.outputs`。
等待用户输入返回 `code=0`、`data.event=user_inputs`，保留 `data.inputs/tips`，
`data.content` 为本轮已输出的提示内容；它表示暂停等待，调用方填表后用同一 session_id 继续。
共享执行在落库后才发送缓冲的 message_end，非流式返回优先保留等待输入终态。
首个 started、部分正文和没有终态的 EOF 均不能作为成功返回。
异常、坏事件、非零帧或 strict 失败返回 `{"code":102,"message":"..."}`，不带成功 data。
错误不被包装成 `**ERROR**` 的成功答案。

`stream: true` 保持共享 SSE 事件合同，含 message、workflow_finished、message_end 或
user_inputs；此入口不额外发送 `[DONE]`。strict 失败发送非零 code 的 error 帧后结束，
不产生本次的成功答案或 message_end。失败会话保存用户输入/errors/运行 DSL，
成功运行完整保存消息与结果；生成器在完成、出错和关闭时均释放。

缺失/无效 beta 凭据返回 HTTP 401、`retcode=109`、`retmsg`、`data=false`。
有效 beta token 的非 owner 首次运行沿用既有拒绝合同：HTTP 200，非流式为非零
SDK code，流式为非零 SSE code；不会执行 Canvas 或创建会话。
此节是活动 agentbots 路由；未注册的 `/agents/{id}/completions` 仍返回 404。

## 对话 API

### 上传运行时附件

**POST** `/documents/upload`（完整路径 `/api/v1/documents/upload`）。

上传聊天、Agent 或 MCP 输入用的附件，返回运行时文件元数据。
文件存入已认证 owner 的 `<owner>-downloads` 空间；本接口不创建 dataset 文档，
不启动解析或索引。聊天取件时才由附件消费者读取并解析文件。

认证使用 `Authorization: Bearer <token>`，接受 web 会话 JWT 或 SDK API Key。
两种凭据都需要有效用户及个人 owner membership；`created_by` 由服务器确定，
不能通过请求指定 owner、storage key 或 dataset。

| 输入 | 规则 |
|---|---|
| multipart `file` | 一个或多个文件，多个文件重复使用字段名 `file`；每个字段须有文件名 |
| query `url` | 单个 HTTP/HTTPS URL，与 `file` 互斥，必须提供其中一种输入 |

例如上传两个文件：

```bash
curl -X POST 'http://localhost:8123/api/v1/documents/upload' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@notes.txt' -F 'file=@image.png'
```

单文件或单 URL 的 `data` 为对象，多文件为对象数组，文件顺序与输入相同。
成功体为 `{"code":0,"data":...}`，每个对象具有以下字段：

| 字段 | 含义 |
|---|---|
| `id` | 随机存储 location，供附件取件使用；不是 dataset document ID |
| `name`, `extension`, `mime_type` | 文件名、扩展名及内容类型；URL 生成 PDF 时使用 `application/pdf` |
| `size` | 实际存储字节数，PDF 修复后按修复结果计算 |
| `created_by`, `created_at` | 服务器认证 owner ID、Unix 时间秒数 |
| `preview_url` | 当前为 `null` |

缺输入、混合输入、空文件字段、不安全 URL 或失败的 URL 抓取返回 HTTP 200、
`code=101`、`message`，不返回成功 `data`。存储及其他执行失败为 `code=100`。
缺失/无效凭据、失效 owner membership 返回 HTTP 401、`code=401`。
调用方须同时检查 HTTP 状态和业务码。

多个文件中途失败时，service 对本次已写对象执行补偿删除并检查存在状态；
同时删除本批服务器描述登记，不会删除历史对象。描述登记失败也会补偿对应文件。
无法确认清理时仍返回非零业务码，并明确说明 cleanup 未能确认。
取消请求会等待正在进行的写入后补偿；这不是对象存储事务，进程被强制终止、
存储不可达时不能保证补偿完成。

URL 的初始地址、DNS 与每个 HTTP redirect 都校验；浏览器 HTTP 请求通过
DNS 绑定的转发取件，浏览器自己的其他网络流量走拒绝连接的代理。
内网/保留地址、无法验证的目的地及 WebSocket 被阻断；只转发 GET/HEAD，
不转发浏览器 cookie/Authorization，也不使用环境代理替代 DNS 绑定。
URL 模式生成 PDF 或网页 Markdown，仍需可用的 Chromium 运行资源。

MCP 聊天 `POST /v1/llm/enhanced_chat_sse` 的 `files` 保持文件 ID 字符串数组，
例如 `{"files":["上传响应中的id"], ...}`。服务器在当前认证用户的 downloads 空间
恢复上传时登记的描述，读取对象并通过 Canvas/FileService 解析；不能由客户端
`created_by`、自造描述或消息中的 URL 选择其他 owner。文字进入本轮用户消息，
图片通过现有模型 `images` 路径传递。普通/结构化、工具/无工具和非流式使用同一取件逻辑。
每次调用清空旧附件；历史上传没有可信登记时须重新上传，不能凭 ID 猜测所属用户。

附件 ID 不存在、属于其他用户、描述/对象缺失或解析失败时，聊天在发送 SSE 前返回
HTTP 400 和 `detail`。开始回答后发生模型等执行错误，SSE 发送 `retcode=500` 后终止，
不再发送 `retcode=0, data=true` 的成功完成帧；非流式执行失败返回 HTTP 500。
成功的 SSE 帧格式保持原协议。

`POST /v1/document/upload_info` 已移除；聊天附件上传使用
`POST /api/v1/documents/upload` 和上述 `file`、`code/data` 合同。
旧路径返回 HTTP 404、`code=404`、`data=null`，不再出现在 OpenAPI 中。
SDK 已有 `POST /api/v1/files/upload_info` 继续使用 multipart 字段 `files` 及
`code/data` 响应，与新入口复用上传 service。

旧的临时 URL/文件转文本入口 `POST /v1/document/parse` 已移除，返回 HTTP 404、
`code=404`、`data=null`，OpenAPI 不再提供该操作。
`POST /api/v1/datasets/{dataset_id}/documents/parse` 继续接收 JSON `document_ids`，
调度数据集文档的异步解析任务；它不返回临时文件的文本内容。
会话解析入库入口 `POST /v1/document/upload_and_parse` 已移除，所有请求均返回
HTTP 404、`code=404`、`data=null`，OpenAPI 不再提供该操作，不上传、解析或调度任务。
该入口原来把文件写入会话绑定的知识库并同步完成解析入库；这项能力已退役。
运行时附件入口返回可信附件描述，继续支持聊天内容提取，它不会自动执行上述会话入库流程。
Explore 和 MCP 页面共用上传状态作为附件列表、取消和发送资格的唯一来源。移除会取消
本次上传请求，迟到结果不恢复卡片；失败或已移除的附件不进入后续聊天请求。
Explore 保留重试，MCP 沿用现有无重试 UI。浏览器移除/取消不承诺删除服务端已经登记的
运行时对象；成功聊天仍按上述可信描述取件与提取合同处理。
写作参考资料 `POST /v1/write/api/reference-materials/parse` 仍接收 `chapter_id` 和
`file`，通过共享文件解析服务生成文本并保存参考资料；成功返回 `retcode=0` 和资料摘要。

### 创建聊天会话

为指定的聊天助手创建一个新的会话。

**请求**

```
POST /chats/{chat_id}/sessions
```

**请求体**

```json
{
  "name": "New session",
  "user_id": "string"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| name | string | 否 | 会话名称，默认 `New session` |
| user_id | string | 否 | 业务侧用户标识，默认空字符串 |

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "session-uuid",
    "name": "New Chat",
    "chat_id": "chat-uuid",
    "messages": [
      {
        "role": "assistant",
        "content": "Hi! I'm your assistant. What can I do for you?"
      }
    ]
  }
}
```

### 列出聊天会话

列出指定聊天助手下的会话。

**请求**

```
GET /chats/{chat_id}/sessions?page=1&page_size=30&orderby=create_time&desc=true&name=&id=&user_id=
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| page | integer | 否 | 页码，默认 1 |
| page_size | integer | 否 | 每页数量，默认 30；传 0 时不分页 |
| orderby | string | 否 | 排序字段，默认 `create_time` |
| desc | boolean | 否 | 是否倒序，默认 true |
| name | string | 否 | 按会话名称过滤 |
| id | string | 否 | 按会话 ID 过滤 |
| user_id | string | 否 | 按业务侧用户标识过滤 |

### 获取聊天会话

获取指定聊天助手下的单个会话。

**请求**

```
GET /chats/{chat_id}/sessions/{session_id}
```

### 更新聊天会话

更新指定会话名称。

**请求**

```
PUT /chats/{chat_id}/sessions/{session_id}
```

**请求体**

```json
{
  "name": "Updated session name"
}
```

`messages` 和 `reference` 不允许通过该接口修改。

### 删除聊天会话

批量删除指定聊天助手下的会话。

**请求**

```
DELETE /chats/{chat_id}/sessions
```

**请求体**

```json
{
  "ids": ["session-uuid"],
  "delete_all": false
}
```

### 删除会话消息

删除指定会话中的一条用户消息及其对应助手回复。

**请求**

```
DELETE /chats/{chat_id}/sessions/{session_id}/messages/{msg_id}
```

### 更新消息反馈

更新指定助手消息的点赞或反馈。

**请求**

```
PUT /chats/{chat_id}/sessions/{session_id}/messages/{msg_id}/feedback
```

**请求体**

```json
{
  "thumbup": false,
  "feedback": "The answer is not accurate."
}
```

### 会话补全

基于指定会话继续生成回答。

**请求**

```
POST /chats/{chat_id}/sessions/{session_id}/completions
```

**请求体**

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Hello"
    }
  ],
  "stream": true
}
```

### 对话补全

与助手进行对话（流式或非流式）。

**请求**

```
POST /chats/{chat_id}/completions
```

**请求体**

```json
{
  "question": "Hello, how are you?",
  "session_id": "string",
  "stream": true,
  "metadata_condition": {
    "logic": "and",
    "conditions": []
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| question | string | 否 | 用户问题 |
| session_id | string | 否 | 对话会话 ID |
| stream | boolean | 否 | 是否流式输出，默认 true |
| metadata_condition | object | 否 | 元数据过滤条件 |

**非流式响应**

```json
{
  "code": 0,
  "data": {
    "id": "completion-uuid",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "I'm doing well, thank you!"
        },
        "finish_reason": "stop"
      }
    ],
    "references": [
      {
        "chunk_id": "chunk-uuid",
        "content": "Referenced content...",
        "document_name": "document.pdf",
        "score": 0.85
      }
    ]
  }
}
```

**OpenAI 兼容补全**

使用已有聊天助手的 ID、API Key 和 OpenAI 风格的 `messages` 请求：

```
POST /openai/{chat_id}/chat/completions
```

`model` 必填；传 `"model"` 使用聊天助手已配置的模型，传具体模型名（例如 `glm-4-flash@ZHIPU-AI`）时必须是当前租户可用的聊天模型。`messages` 最后一条必须为用户消息；文本数组内容支持 `type: "text"`，图片等非文本内容会返回参数错误。省略 `stream` 时新路径返回非流式响应；设为 `true` 则返回 SSE。旧路径 `/chats_openai/{chat_id}/chat/completions` 暂保留，省略 `stream` 时仍默认流式，已标记 deprecated。

可在 `extra_body` 中设置 `reference: true`、`reference_metadata: {"include": true, "fields": ["author"]}` 和 `metadata_condition`。非流式引用位于 `choices[0].message.reference`；流式引用及完整最终正文位于收尾帧的 `choices[0].delta.reference` 和 `final_content`。引用切片的 `document_metadata` 只包含请求的字段。

Python OpenAI 客户端的 `base_url` 应设为 `http://<your-server>:8123/api/v1/openai/{chat_id}`，再调用 `client.chat.completions.create(...)`；客户端会自行追加 `/chat/completions`。客户端还会把 `extra_body` 中的 `reference` 等参数合并到请求 JSON 顶层；直接发送 HTTP 时也可将这些参数放在 JSON 的 `extra_body` 对象中，无需双重嵌套。

**流式响应 (SSE)**

```
data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":"I'm"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":" doing"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":" well"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}

data: [DONE]
```

## 知识库 API

### 列出知识库

获取当前用户的所有知识库。

**请求**

```
GET /datasets
```

**查询参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| page | integer | 页码，默认 1 |
| page_size | integer | 每页数量，默认 20 |
| name | string | 按名称搜索 |

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 10,
    "items": [
      {
        "id": "dataset-uuid",
        "name": "My Knowledge Base",
        "description": "Description",
        "document_count": 5,
        "chunk_count": 100,
        "embedding_model": "BAAI/bge-m3",
        "created_at": "2024-01-01T00:00:00Z"
      }
    ]
  }
}
```

### 创建知识库

创建一个新的知识库。

**请求**

```
POST /datasets
```

**请求体**

```json
{
  "name": "string",
  "description": "string",
  "embedding_model": "string",
  "chunk_method": "string",
  "parser_config": {
    "chunk_token_num": 512,
    "layout_recognize": true,
    "parent_child": {
      "use_parent_child": true,
      "children_delimiter": "\n"
    }
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 是 | 知识库名称 |
| description | string | 否 | 描述 |
| embedding_model | string | 否 | Embedding 模型 |
| chunk_method | string | 否 | 分块方法：naive, manual, qa, etc. |
| parser_config | object | 否 | 解析配置 |

`parser_config.parent_child` 用于启用 parent-child 分块。启用后，系统会先按普通配置生成父分块，再用 `children_delimiter` 将父分块拆成更小的子分块用于向量匹配；检索命中子分块时，会把父分块全文作为上下文返回给大模型。

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| parser_config.parent_child.use_parent_child | boolean | 否 | 是否启用 parent-child 分块，默认 `false` |
| parser_config.parent_child.children_delimiter | string | 否 | 子分块分隔符，默认 `"\n"`，仅在 `use_parent_child=true` 时生效 |

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "dataset-uuid",
    "name": "My Knowledge Base",
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

### 更新知识库

更新指定知识库的基础信息、分块方法或解析配置。

**请求**

```
PUT /datasets/{dataset_id}
```

**请求体**

```json
{
  "name": "string",
  "description": "string",
  "embedding_model": "string",
  "chunk_method": "naive",
  "parser_config": {
    "parent_child": {
      "use_parent_child": true,
      "children_delimiter": "\n"
    }
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| dataset_id | string | 是 | 知识库 ID |
| name | string | 否 | 知识库名称 |
| description | string | 否 | 描述 |
| embedding_model | string | 否 | Embedding 模型；已有分块时不能切换 |
| chunk_method | string | 否 | 分块方法：naive, manual, qa, etc. |
| parser_config | object | 否 | 解析配置；支持 `parent_child` 嵌套配置 |

`parser_config.parent_child` 的字段含义与创建知识库相同。设置 `use_parent_child=false` 时，会清空执行层使用的 `children_delimiter`。

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "dataset-uuid",
    "name": "My Knowledge Base",
    "updated_at": "2024-01-01T00:00:00Z"
  }
}
```

### 删除知识库

删除指定的知识库。

**请求**

```
DELETE /datasets/{dataset_id}
```

**响应**

```json
{
  "code": 0,
  "message": "success"
}
```

### 元数据模板配置

以下接口接受当前会话 JWT 或 API Key。模板写入 `parser_config.metadata`，保留
其他解析配置（包括 RAPTOR）；它不直接修改已解析分块的元数据值。

| 方法与路径（均以 `/api/v1` 开头） | 请求及权限 |
|---|---|
| `GET /datasets/{dataset_id}/metadata/config` | 数据集 owner；返回 `data.enabled` 和 `data.fields` |
| `PUT /datasets/{dataset_id}/metadata/config` | 数据集 owner；`{"enabled":true,"fields":[{"key":"author","description":"作者"}]}`；空 fields 合法，`{}` 使用 enabled=true、fields=[] 的默认值 |
| `PUT /datasets/{dataset_id}/documents/{document_id}/metadata/config` | 当前数据集 owner/admin；`{"metadata":[{"key":"author"}]}`，也接受 JSON schema 对象；`{"metadata":[]}` 清空模板 |

文档必须属于路径中的数据集。成功为 HTTP 200、`code:0`；不存在/跨数据集为
`code:102`，文档写入权限不足为 `code:109`。数据集配置保持 owner 权限，不开放给
团队 admin。缺请求体或缺文档 metadata 字段为 HTTP 422、`detail` 验证错误列表。
缺失/无效凭证为 HTTP 401、`retcode:109`、`retmsg`、`data:false`；调用方应分别检查
HTTP 状态与对应业务码。

### 数据集摄取日志

`GET /api/v1/datasets/{dataset_id}/ingestions` 按现行数据集访问权限读取数据集级
GraphRAG/RAPTOR/MindMap 日志，成功返回 `{"code":0,"data":{"total":0,"logs":[]}}`。
它与 Web 文件日志 `POST /v1/kb/list_pipeline_logs` 的筛选范围不同。

| Query 参数 | 含义 |
|---|---|
| `create_date_from`、`create_date_to` | ISO 日期或 datetime，含边界；可单独使用。无时区按 UTC，显式时区先换算 UTC |
| `operation_status` | 可重复的状态筛选参数 |
| `orderby`、`desc` | 默认 create_time、true；orderby 必须为日志响应字段 |
| `page`、`page_size` | 均为非负整数；同时非零时分页，page 从 1 开始；total 为筛选后未分页的条数 |

换算后起始时间晚于结束时间，返回 HTTP 200、`code:102` 和
`message:"create_date_from must not be later than create_date_to"`，不会返回成功空列表。
相等时间合法。日期格式错误或负页码为 HTTP 422、`detail` 验证错误列表；不存在或
无权限的数据集为 `code:102`、`message:"No authorization."`，鉴权错误沿用上节 HTTP 401
信封。`GET /api/v1/datasets/{dataset_id}/ingestions/{log_id}` 不返回其他数据集的日志。

## 文档 API

### 批量更改文档启用状态

`POST /api/v1/datasets/{dataset_id}/documents/batch-update-status`

JWT 或 API Key 鉴权。有效数据集的 owner/admin 可写；当前租户 owner 身份沿用
user ID 等于 tenant ID 的规则，其余成员要求有效 owner/admin membership。
normal、invite、失效成员和外部用户不能修改。路径数据集必须包含目标文档；
文档 status=0 表示禁用，仍可重新启用。

请求：

```json
{"doc_ids":["document_id_1","document_id_2"],"status":0}
```

status 只接受整数 0/1 或字符串 "0"/"1"；布尔、浮点数、空 ID、非字符串 ID、
空列表、错误形状及额外字段返回 HTTP 422。缺失或无写权限的数据集返回 HTTP 200、
业务 code=109。每个 ID 独立处理，重复 ID 只处理一次；完整成功 code=0，
部分失败 code=500，并保留完整 data 映射：

```json
{"code":500,"message":"Partial failure","data":{"document_id_1":{"status":"0"},"document_id_2":{"error":"Document not found in this dataset."}}}
```

即使 SQL 已是目标状态也会重新对齐索引。同文档状态写入和普通 source 写入使用
SQL 行锁排序；索引完成后才提交 SQL，失败尝试按当前 SQL 状态恢复索引。
恢复无法确认时返回安全错误，调用方可重试。没有分块的文档更新 SQL，后续普通
source 分块按写入前的最新状态插入；母块保持隐藏，图谱/RAPTOR 等产品保留原语义。
ES/OpenSearch availability 更新要求完整响应：超时、版本冲突、失败项、缺表、未知或部分更新
都不算成功；全部 updated/noops 与 total 对齐时接受，同状态 no-op 可以成功。
Infinity 与 ES/OpenSearch 启用、同状态修复及补偿保持母块隐藏。按同数据集、同文档的
完整父子引用识别旧母块（自身 mom_id 缺失或为空），同时支持新母块自身 mom_id=id；
普通无 mom_id 切片仍按目标启用。关系读取失败、不完整或重复时不执行启用写入；
ES/OpenSearch 使用完整 scroll 读回并关闭游标，Infinity 核对关系行数与实际计数。
这是跨存储补偿合同，进程崩溃和外部直接改写索引不构成分布式原子提交保证。

Go 使用同一路径及响应合同，接受当前 Python HS256 JWT（同服务签名密钥）和
现有 API/login token。用户须有效、活动、已认证且非匿名，并具有唯一有效个人 owner
成员及个人租户；JWT 登出标记 INVALID_ 会拒绝旧 JWT，API key 不由该 JWT 登出标记失效。
Infinity 每文档使用独占连接完成计数、写入及恢复，排空后关闭，避免共享 Thrift 并发。
Infinity 支持实际索引写入；Go ES/Milvus 当前无状态写入
基盘，有索引时返回逐文档非零错误且 SQL 不变。无表、无分块场景可仅更新 SQL；
Milvus 有集合但无法判定当前文档是否有分块时同样明确拒绝。

Web 单条/批量启停已迁至上述 dataset REST 并完成真实请求与存储读回验收。
旧 `POST /v1/document/change_status` 及专用请求模型已移除：合法或非法 body、
任何凭据均返回 HTTP 404、`code=404`、`data=null`，OpenAPI 不再提供该操作。
新 batch、PATCH enabled 与共享状态/source 服务继续使用上述合同。

### 批量提交、取消或重置文档解析

`POST /api/v1/documents/ingest` 使用 JWT 或 API Key 鉴权。每个目标文档须属于
当前用户可写的活动知识库，权限沿用 owner/admin 规则；禁用文档仍可提交解析，
新普通 source 分块继承当前 SQL status，母块保持隐藏。

```json
{"doc_ids":["document_id_1","document_id_2"],"run":1,"delete":false,"apply_kb":false}
```

`doc_ids` 必须是非空字符串数组，按首次出现顺序去重。`run` 只接受整数 0/1/2
或精确字符串 "0"/"1"/"2"；布尔、浮点数、空 ID、错误形状及额外字段返回
HTTP 422。`delete`、`apply_kb` 只接受布尔值，默认均为 false；`apply_kb=true`
仅可用于 run=1。全请求权限预检先于任何文档变更。

| run | 行为 |
|---|---|
| 1 | 提交或复用解析任务；默认保留历史，`delete=true` 清理解析历史后重新提交 |
| 2 | 提交活动文档及未完成任务的取消请求；默认保留已有切片和计数 |
| 0 | 重置任务状态，不排队；默认保留已完成历史 |

清理历史会对齐索引、任务和 Document/Knowledgebase 计数，不删除 Document、File
或源对象。`apply_kb=true` 只继承知识库的 `llm_id`、`enable_metadata`、`metadata`
三个字段，保留文档其余 parser、RAPTOR、GraphRAG 和 pipeline 配置。

完整成功为 HTTP 200、整数 `code=0`、`data=true`；这表示提交或复用已确认，
不表示后台解析 DONE，取消确认也不表示 worker 已排空。业务拒绝仍使用 HTTP 200：
权限失败 code=109、状态冲突 code=102，部分失败或内部失败 code=500。逐文档执行
失败时，`data.results` 保留每个去重请求 ID 的真实结果，例如：

```json
{"code":500,"message":"Document ingestion was not fully submitted.","data":{"results":{"document_id_1":{"run":"1"},"document_id_2":{"error":"Document could not be ingested."}}}}
```

失败项可能另有 `queued_task_ids`、`uncertain_task_ids` 或副作用说明；有 error 的项
不能当作整项成功。缺失或未知结果须重新读回，不能因 HTTP 200 或某个 queue ID
清除失败选择。等价的完整任务计划已确认排队且尚未开始时，重试复用 Task ID，
不重复排队；已开始的重新解析产生新任务世代。失败补偿使用持久恢复材料，按实际
SQL、队列归属和当前状态协调，后来任务世代的写入不会被旧任务的迟到写入撤销。
这不构成跨 SQL、索引和队列的分布式原子提交保证。

现有 dataset `documents/parse` 保持清理历史、不开启 apply_kb 的默认行为；
`documents/stop` 默认保留历史。admin 两个实际解析调用已迁至 ingest，SYNC 仅在
本次提交 ID 全部读回 DONE 时完成。Web 普通解析/停止继续使用 dataset canonical 接口，
显式重新解析的保留/清理历史分支统一使用 ingest，并传递用户选择的 `apply_kb`；
部分失败保留失败文档的选择和选项。Web 与 admin 迁移已完成真实验收，旧
`POST /v1/document/run` 仍保留 deprecated，待全部消费者复核后单独退出。

### 读取缩略图和图片

以下三个只读接口使用 JWT 或 API Key 鉴权：

| 请求 | 返回与授权 |
|---|---|
| `GET /api/v1/thumbnails?doc_ids=id1&doc_ids=id2` | HTTP200、`code:0`、`data` 为文档 ID 到缩略图 URL 的映射；最多100个原始 ID，重复也计入上限。过滤不存在、无权读取或知识库停用的文档；保留 inline `data:image/*`、null 和空串。 |
| `GET /api/v1/documents/images/{image_id}` | 读取可访问知识库中的精确 SQL thumbnail，或由同知识库索引与 Document 登记的图片；禁用文档的登记图片仍可读取。 |
| `GET /api/v1/documents/runtime/{file_id}/image` | 读取上传时登记在当前认证 owner downloads 空间的运行时附件，校验可信 sidecar 与实际大小；请求中的 owner、created_by、preview_url 不构成授权。 |

binary 成功响应为完整原始 PNG、JPEG、GIF、WebP 或 BMP 字节，`Content-Type` 与实际格式一致。
`image_id` 的首个 hyphen 分隔知识库和对象 key；对象 key 中的空间、percent、Unicode、hyphen
及 slash 由返回的 canonical URL 编码一次，调用方应使用该 URL。REST 和旧文档列表均返回新 URL。
这些 GET 不提交解析，不写 SQL、索引、对象或队列；所有新接口的成功与失败响应均带
`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。

新接口失败为安全 JSON `{code, message, data:null}`：

| HTTP | code | 含义 |
|---|---|---|
| 422 | 101 | query 形状或必填字段无效 |
| 400 | 101 | 图片输入无效 |
| 401 / 403 | 401 / 109 | 未认证或认证依赖拒绝；保留安全认证 challenge |
| 404 | 102 | 图片不可用；包含无权访问、未登记和缺失情况，不表示已经判定物理对象不存在 |
| 415 | 102 | 实际内容不是完整受支持的 raster 图片 |
| 500 | 500 | 存储或服务器读取失败 |

旧 `GET /v1/document/thumbnails` 已移除，返回 routing404。旧 binary
`GET /v1/document/image/{image_id}` 暂保留 deprecated 的原有公开读取、JPEG MIME 和错误格式，
Agent Hub 已完成新接口的真实认证、完整字节和页面迁移验收；Web 消费者迁移仍在进行。
旧 binary 待双方迁移接受及全部消费者复核后单独退出，不能据新接口鉴权声称旧入口已经退出。

### 下载代码沙箱产物

CodeExec 生成的 Markdown 附件链接指向以下 REST 路径：

```
GET /documents/artifact/{filename}
```

完整地址为 `/api/v1/documents/artifact/{filename}`；旧地址
`/v1/document/artifact/{filename}` 暂作 deprecated 兼容入口。两者均需登录令牌或
个人 API Key。`filename` 为生成时的 32 位小写十六进制 ID，加
`.png`、`.jpg`、`.jpeg`、`.svg`、`.pdf`、`.csv`、`.json` 或 `.html` 扩展名。
CodeExec 生成的 URL 带 `run_id`；有持久化 Agent 会话时另带 `session_id`，例如
`/api/v1/documents/artifact/<filename>?run_id=<run_id>&session_id=<session_id>`。
服务端在上传时登记文件名与请求用户、运行 ID、可选会话 ID 的精确绑定；下载时检查该登记，
会话运行还要求会话存在且 Canvas 可访问。只提供一个属于自己的 `session_id`，或让助手消息
引用其他人的文件 URL，均不能取得该对象。流式链接在登记完成后即可下载，无需等待助手消息落库。
历史上未登记的裸链接无法证明归属，不提供无鉴权兜底。成功时响应为原始文件字节，
`Content-Type` 按扩展名设置，HTML 与 SVG 强制作为附件下载。
无权访问、文件不存在或文件名无效时检查非零业务 `retcode`，不能只看 HTTP 200。

### 创建文档

上传本地文件、抓取网页为 PDF，或创建空白虚拟文档。创建后不会自动开始解析。

**请求**

```
POST /datasets/{dataset_id}/documents?type=local|web|empty
```

`type` 可省略，默认为 `local`。

| type | 请求体 | 必需字段 | 成功时的 `data` |
|------|--------|----------|-----------------|
| local | multipart/form-data | `file` 或兼容字段 `files`，可上传多个 | 文档数组 |
| web | multipart/form-data | `name`、`url` | 单个文档 |
| empty | application/json | `{"name": "blank.txt"}` | 单个文档 |

`local` 可使用可选表单字段 `parent_path`，并支持 `return_raw_files=true` 查询参数返回原始文档字段。
网页 URL 会经过出网地址校验；各模式均检查数据集访问权限。失败时检查响应中的非零业务 `code`。

**请求示例**

```bash
curl -X POST 'http://{address}/api/v1/datasets/{dataset_id}/documents?type=web' \
  -H 'Authorization: Bearer <API_KEY>' \
  -F 'name=example-page' -F 'url=https://example.com'

curl -X POST 'http://{address}/api/v1/datasets/{dataset_id}/documents?type=empty' \
  -H 'Authorization: Bearer <API_KEY>' -H 'Content-Type: application/json' \
  -d '{"name":"blank.txt"}'
```

**成功响应示例**（`web`、`empty`；`local` 的 `data` 为文档数组）

```json
{
  "code": 0,
  "data": {
    "id": "document-uuid",
    "name": "example-page.pdf",
    "dataset_id": "dataset-uuid",
    "run": "UNSTART"
  }
}
```

### 列出文档

获取知识库中的文档列表。

**请求**

```
GET /datasets/{dataset_id}/documents
```

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 5,
    "items": [
      {
        "id": "document-uuid",
        "name": "document.pdf",
        "size": 1024000,
        "status": "done",
        "chunk_count": 20,
        "progress": 1.0,
        "created_at": "2024-01-01T00:00:00Z"
      }
    ]
  }
}
```

### 解析文档

开始解析文档。

**请求**

```
POST /datasets/{dataset_id}/documents/{document_id}/run
```

**响应**

```json
{
  "code": 0,
  "message": "success"
}
```

### 删除文档

删除指定的文档。

**请求**

```
DELETE /datasets/{dataset_id}/documents/{document_id}
```

## 分块 API

### 列出分块

获取文档的分块列表。

**请求**

```
GET /datasets/{dataset_id}/documents/{document_id}/chunks
```

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 20,
    "items": [
      {
        "id": "chunk-uuid",
        "content": "Chunk content...",
        "important_keywords": ["keyword1", "keyword2"],
        "tag_kwd": ["tag1", "tag2"],
        "position": 1
      }
    ]
  }
}
```

### 添加分块

向指定文档添加新的分块，可附加 Base64 编码图片。

**请求**

```
POST /datasets/{dataset_id}/documents/{document_id}/chunks
```

**请求体**

```json
{
  "content": "Chunk content...",
  "important_keywords": ["keyword1"],
  "tag_kwd": ["tag1", "tag2"],
  "image_base64": "<base64-encoded-image>"
}
```

**响应**

```json
{
  "code": 0,
  "data": {
    "chunk": {
      "id": "chunk-uuid",
      "content": "Chunk content...",
      "important_keywords": ["keyword1"],
      "tag_kwd": ["tag1", "tag2"],
      "image_id": "dataset-uuid-chunk-uuid"
    }
  }
}
```

### 更新分块

更新分块内容或关键词。

**请求**

```
PUT /datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}
```

**请求体**

```json
{
  "content": "Updated content...",
  "important_keywords": ["new", "keywords"],
  "tag_kwd": ["tag3"]
}
```

## 检索 API

### 检索测试

在知识库中进行检索测试。

**请求**

```
POST /retrieval
```

**请求体**

```json
{
  "dataset_ids": ["dataset-uuid"],
  "question": "What is RAG?",
  "top_k": 5,
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.3
}
```

**响应**

```json
{
  "code": 0,
  "data": {
    "chunks": [
      {
        "id": "chunk-uuid",
        "content": "RAG (Retrieval-Augmented Generation)...",
        "tag_kwd": ["tag1", "tag2"],
        "score": 0.85,
        "document_name": "rag_intro.pdf"
      }
    ]
  }
}
```

## 助手 API

### 列出助手

获取所有对话助手。

**请求**

```
GET /chats
```

**查询参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| id | string | 按聊天助手 ID 精确过滤 |
| name | string | 按名称精确过滤 |
| keywords | string | 按关键词搜索 |
| page | integer | 页码；为 0 时不分页 |
| page_size | integer | 每页数量；为 0 时不分页 |
| orderby | string | 排序字段，默认 `create_time` |
| desc | boolean | 是否降序，默认 true |

### 创建助手

创建一个新的对话助手。

**请求**

```
POST /chats
```

**请求体**

```json
{
  "name": "My Assistant",
  "dataset_ids": ["dataset-uuid"],
  "llm_id": "glm-4-plus@ZHIPU-AI",
  "llm_setting": {
    "model_type": "chat",
    "temperature": 0.1
  },
  "prompt_config": {
    "system": "You are a helpful assistant.",
    "prologue": "Hi! I'm your assistant. What can I do for you?",
    "parameters": [{"key": "knowledge", "optional": false}],
    "empty_response": "Sorry! No relevant content was found in the knowledge base!",
    "quote": true
  },
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.3,
  "top_n": 6,
  "top_k": 1024,
  "rerank_id": ""
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 是 | 聊天助手名称 |
| dataset_ids | array | 否 | 关联的知识库 ID；省略或为空数组时创建空助手，可稍后绑定知识库 |
| llm_id | string | 否 | 聊天模型 ID；未指定时使用租户默认聊天模型 |
| llm_setting | object | 否 | 模型参数配置，例如 `model_type`、`temperature`、`top_p`、`presence_penalty`、`frequency_penalty` |
| prompt_config | object | 否 | 提示词配置，例如 `system`、`prologue`、`parameters`、`empty_response`、`quote`、`tts`、`refine_multiturn` |
| similarity_threshold | number | 否 | 相似度阈值 |
| vector_similarity_weight | number | 否 | 向量相似度权重 |
| top_n | integer | 否 | 送入回答生成的分块数量 |
| top_k | integer | 否 | 召回候选数量 |
| rerank_id | string | 否 | Rerank 模型 ID |

### 获取助手

获取指定聊天助手配置。

**请求**

```
GET /chats/{chat_id}
```

### 全量更新助手

覆盖指定聊天助手的配置。

**请求**

```
PUT /chats/{chat_id}
```

`PUT` 适用于提交完整配置。请求体中省略的字段不会进行嵌套合并，可能被服务端默认值或空值覆盖；只改少数字段时应使用 `PATCH`。

### 部分更新助手

只更新指定字段。

**请求**

```
PATCH /chats/{chat_id}
```

`PATCH` 会保留未提供的字段，并对 `llm_setting`、`prompt_config` 这类嵌套对象做浅层合并，适合重命名助手或只调整部分模型/提示词参数。

### 删除助手

删除单个聊天助手。

**请求**

```
DELETE /chats/{chat_id}
```

### 批量删除助手

按 ID 批量删除聊天助手，或在 `delete_all` 为 true 时删除当前用户的全部助手。

**请求**

```
DELETE /chats
```

**请求体**

```json
{
  "ids": ["chat-uuid-1", "chat-uuid-2"],
  "delete_all": false
}
```

## Agent API

### 执行 Agent

执行 Agent 工作流。

**请求**

```
POST /agents/{agent_id}/run
```

**请求体**

```json
{
  "inputs": {
    "query": "User input..."
  },
  "stream": true
}
```

## Go Provider API（并行实现）

本节描述 Go `internal/` 的独立实现；当前 Web 模型页仍使用 Python `/v1/llm/*`。
基础路径为 `/api/v1/providers`，现有认证与用户/tenant 约束继续适用。

| 路径 | 行为 |
|---|---|
| `POST /{provider_name}/instances/{instance_name}/models` | 普通 JSON 或 sender SSE；`thinking` 省略时使用所选模型默认，显式 true/false 优先 |
| `GET /{provider_name}/instances/{instance_name}/connection` | Google 通过实际模型分页列表检查连接 |
| `GET /{provider_name}/instances/{instance_name}/models?supported=true` | 返回 provider 支持的模型名；Google 遍历全部页，保留 `models/` 前缀 |

Google 使用 Gemini genai SDK，BaseURL 依次按 region/空 region 与 default 选择。
JSON 成功继续返回 `code`、`answer`、`reasoning_content`；sender 先发 `[REASONING]`
再发 `[MESSAGE]`，实际文本答案成功后才发完成帧。provider/SQL/写流错误为安全非零
JSON 或错误 SSE，取消传到 SDK 请求；空候选/空内容不会伪成功。Google embedding、
余额与旧 channel-only streaming 明确不支持。实际本地验收采用受控身份和 provider
HTTP，不表示远程 Google 账号、生产身份、完整 worker 或 Web 页面已验收。

## 系统 API

系统 API 使用本文档的基础 URL，即 `/api/v1`。旧版 `/v1/system/*` 路由仍可用于兼容历史客户端，并已在 OpenAPI 中标记为 deprecated；新集成应优先使用本节 RESTful 路径。

### 连通测试

检查 MultiRAG 服务是否可访问。

**请求**

```
GET /system/ping
```

**响应**

```
pong
```

### 获取系统版本

获取当前服务版本。

**请求**

```
GET /system/version
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": "0.9.9"
}
```

### 检查系统健康状态

检查数据库、Redis、文档引擎、对象存储和聊天服务等关键依赖的健康状态。该接口不需要 API Key。

**请求**

```
GET /system/healthz
```

**状态码**

| 状态码 | 说明 |
|--------|------|
| 200 | 所有关键依赖正常 |
| 500 | 至少一个关键依赖异常 |

**响应示例**

```json
{
  "database": {
    "status": "green",
    "elapsed": "3.2"
  },
  "redis": {
    "status": "green",
    "elapsed": "1.1"
  },
  "storage": {
    "status": "green",
    "elapsed": "5.6"
  }
}
```

### 列出 API Tokens

列出当前登录用户所属 owner 租户下的 API Tokens。

**请求**

```
GET /system/tokens
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": [
    {
      "tenant_id": "tenant-uuid",
      "name": "API Token",
      "description": null,
      "token": "multirag-...",
      "beta": "abcdef1234567890abcdef1234567890"
    }
  ]
}
```

### 创建 API Token

为当前登录用户所属 owner 租户创建新的 API Token。`name` 可通过 JSON body 或 query 参数传入；未传时默认使用 `API Token`。

**请求**

```
POST /system/tokens
```

**请求体**

```json
{
  "name": "My Token",
  "description": "Used by automation"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 否 | Token 名称，最长 20 个字符 |
| description | string | 否 | Token 描述 |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "tenant_id": "tenant-uuid",
    "name": "My Token",
    "description": "Used by automation",
    "token": "multirag-...",
    "beta": "abcdef1234567890abcdef1234567890"
  }
}
```

### 删除 API Token

删除当前登录用户所属租户下的指定 API Token。

**请求**

```
DELETE /system/tokens/{token}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| token | string | 是 | 要删除的 API Token |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": true
}
```

### 获取日志级别

获取当前运行时日志级别配置。

**请求**

```
GET /config/log
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "root": "INFO",
    "sqlalchemy": "WARNING"
  }
}
```

### 设置日志级别

运行时调整指定包的日志级别。

**请求**

```
PUT /config/log
```

**请求体**

```json
{
  "pkg_name": "core.utils.redis_conn",
  "level": "DEBUG"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| pkg_name | string | 是 | 包名或 `root` |
| level | string | 是 | 日志级别，例如 `DEBUG`、`INFO`、`WARNING`、`ERROR` |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "pkg_name": "core.utils.redis_conn",
    "level": "DEBUG"
  }
}
```

## 健康检查

### 健康状态

检查服务健康状态。

**请求**

```
GET /health
```

**响应**

```json
{
  "status": "ok"
}
```

## 错误码

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1001 | 参数错误 |
| 1002 | 认证失败 |
| 1003 | 权限不足 |
| 1004 | 资源不存在 |
| 1005 | 资源已存在 |
| 2001 | 服务内部错误 |
| 2002 | 服务不可用 |

## OpenAPI 文档

完整的 OpenAPI 规范可通过以下地址访问：

```
http://<your-server>:8123/docs
http://<your-server>:8123/redoc
```

---

有关 Python SDK 的使用，请参阅 [Python API 参考](./python_api_reference.md)。
