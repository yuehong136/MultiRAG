# Memory 查询与来源

Memory 提供结构化检索、原始来源摘录和有预算的查询上下文。所有入口使用服务器认证的用户身份检查个人所有权或当前 team 成员权限；请求中的 `user_id`、`agent_id`、`session_id` 只缩小内容范围，不授予访问权。完整权限规则见 [Memory HTTP 权限](memory-api-authorization.md)。

这些读取操作不会修改记忆、生成新摘要或触发后台提取。
需要保留分段角色、事件时间和认证提交凭据时，使用可选的[结构化来源](memory-structured-sources.md)入口，旧 RAW 合同保持原样。

## 最近消息

`GET /api/v1/messages` 保留数组响应和原有默认行为，增加可选筛选：

| 参数 | 含义 |
| --- | --- |
| `memory_id` | 一个或多个 Memory ID，继续支持逗号分隔 |
| `message_type` | `raw`、`semantic`、`episodic`、`procedural` 之一；省略时不限制类型 |
| `user_id` | 消息主体；与认证用户分别处理 |
| `agent_id`、`session_id` | 保留原有筛选语义 |
| `status` | `true` 或 `false`；省略时不改变原有状态筛选行为 |

需要稳定顺序和分页时，使用单个 Memory 的新入口：

```http
GET /api/v1/memories/{memory_id}/messages?message_type=raw&user_id=subject&status=true&limit=20
```

`data` 为 `{"messages": [...], "next_cursor": "..."}`。后续请求保持相同筛选和 `limit`，添加返回的 `cursor`；最后一页的 `next_cursor` 为 `null`。

新入口按 `valid_at` 降序、数值 `message_id` 降序排列，并对主体、Agent、会话执行精确匹配。具体读取方式由 `capabilities.recent_messages` 返回：

| `pagination` | 读取方式 | 变化检测 |
| --- | --- | --- |
| `live_seek_cursor` | 在已确认支持的 Milvus 3.x / SDK 3.x 上使用原生双字段排序与范围过滤，每次读取当前页及一个后继项 | 续页重读首条和上一页末条边界，边界记录消失或变化时返回 409；不检测整个集合的变化 |
| `bounded_scan_cursor` | 其他当前连接及已启用修订的受管 Memory，通过有界完整读取与资格检查后排序 | 游标绑定完整候选集指纹；集合变化时返回 409 |

原生分页从上一页末尾的 `(valid_at, message_id)` 继续，不使用大 offset。中间记录的并发新增、修改或删除仍可能影响后续结果；已翻过区域的新消息不会自动出现在后续页。它不提供跨请求快照。

有界读取每次最多扫描 10,000 条；达到此上限或无法确认读取完整时返回 422，要求缩小筛选范围，不将不完整集合当成完整分页。

两种游标都绑定授权租户、Memory、筛选与页大小。409 后调用者应重新读取第一页；游标不能代替每次请求的权限检查。升级 SDK 不能单独赋予旧服务端排序能力，连接会核对实际 SDK 与服务端版本。

## 检索附带来源

原有 `GET /api/v1/messages/search` 默认载荷保持不变。添加 `include_sources=true` 可获得每条结果的 `source`：

```http
GET /api/v1/messages/search?memory_id={memory_id}&query=project%20decision&include_sources=true
```

附带来源的查询要求非空查询、最多 100 个不同 Memory ID、`top_n` 为 1–100，权重与阈值在 0–1。返回结果经过授权范围与精确主体筛选；原始来源读取有数量和摘录长度上限。

| `source.state` | 含义 |
| --- | --- |
| `not_applicable` | 结果自身为没有派生父记录的 RAW |
| `available` | 当前可读取，父 RAW 的 Memory、ID、主体、Agent、会话与引用匹配 |
| `missing` | 未找到引用的原始记录 |
| `inactive` | 原始记录已禁用 |
| `forgotten` | 原始记录已设置遗忘时间 |
| `mismatch` | 类型、身份、父引用或必要字段无法匹配 |

这一路径提供诊断，不把已存在的派生记录自动删除。来源不可用时不会附带其正文。`verification=provenance_only` 表示引用关系检查，不证明记忆内容为事实。
显式启用 [修订与来源撤回](memory-revisions.md) 后，正常供给另受持久来源状态和当前版本约束；诊断不能绕过该约束读取撤回正文。

## 分段读取原始来源

```http
GET /api/v1/memories/{memory_id}/sources/{source_id}?offset=0&limit=512
```

仅返回当前有效的真实 RAW。响应包括类型化 `locator`、完整正文的 `content_sha256`、`excerpt`、`offset`、`next_offset`、`total_chars` 和 `truncated`。偏移与长度按 Unicode 码点计算，不按 UTF-8 字节计算；单次最多 8,192 个字符。

续取时带上 `next_offset` 和 `content_sha256`。每次重新检查权限并回读来源；正文变化返回 409。来源被禁用、遗忘、不存在或不匹配筛选时返回 404，不泄露其正文。内容哈希不是不可变修订，也不代替权限或撤回检查。

## 有预算的查询上下文

```http
POST /api/v1/memories/{memory_id}/context
Content-Type: application/json

{
  "mode": "query",
  "query": "What decision applies to this task?",
  "user_id": "subject",
  "budget_tokens": 2048,
  "max_items": 5,
  "candidate_limit": 20,
  "source_excerpt_chars": 512,
  "include_sources": true
}
```

`query` 必须非空；`budget_tokens` 为 128–32,768，`max_items` 和 `candidate_limit` 为 1–100。请求拒绝未知字段，数值预算不接受布尔值。可选 `user_id`、`agent_id`、`session_id` 精确筛选，不进行空白规范化。当前仅支持 `query` 模式。
无需查询词的小份背景使用独立的[只读启动上下文](memory-startup.md)，不改变本接口的默认行为。

服务复用既有 embedding、检索编排与消息存储，逐项核对候选和父 RAW。仅装入当前有效、来源为 `available` 或 `not_applicable` 的完整条目；无法匹配的内容不进入上下文。`include_sources=false` 只隐藏来源摘录，仍执行来源检查。

响应 `data` 包括：

- `items`：选入的结构化条目及来源信息。
- `usage`：`estimated_tokens`、`budget_tokens`、`token_count_is_estimate`。
- `truncated`、`omitted_count`、`deduplicated_count`：本次候选的选择结果，不是全库总数。
- `source_state_counts`：本次候选的来源检查结果。
- `source_snapshot_only`：检查仅对应本次读取。

预算使用项目的 tokenizer 对字面内容计数，特殊 token 标记作为普通来源文本处理；计数失败会让请求失败，不会视为零消耗。计数包含完整领域 JSON（正文、来源摘录、身份、计数及 usage 等包装）。HTTP 的 `retcode/retmsg/data` 信封、调用者添加的提示模板及模型协议开销不在这一预算中。调用者应为这些额外内容预留空间，并在目标模型处再次计量。若连空领域响应都放不下，返回 422。

去重仅针对实际选入的相同结构化条目；不同来源、主体或时间不会仅因正文相似被合并。超预算条目整体省略，不让模型改写其内容。

现有 RAW 将用户和 Agent 文本放在同一正文中，因此返回 `role_status=legacy_role_unknown`；不会根据正文中的标签重建可信角色或完整工具轨迹。来源检查不提供版本发布、全路径级联撤回或回答生成期间的原子快照。已返回给调用者的内容不能通过后续读取追溯收回。

## 会话参考时间

后台提取回读与任务绑定的 RAW 正文及 `valid_at`，将已记录的接收时间作为相对日期的参考。重试不会改用后台处理当天的日期。来源丢失、身份不匹配或时间格式非法时，在调用模型及写入派生记录之前失败。

该时间不证明事件实际发生时刻。提示保留明确事件日期，不猜测缺失的时刻或时区；episodic 的 `invalid_at` 继续表示事件结束时间。没有参考时间的直接提取调用明确标记未提供，不自动用当前时间补齐。

## 能力与错误

`GET /api/v1/memories/{memory_id}/capabilities` 在权限检查后返回可用的读取方式、筛选项、上限与估算/快照边界。它不执行 embedding 或消息查询。

新增入口使用 `retcode/retmsg/data` 信封，并提供 HTTP 状态：200 成功、404 不存在或无权访问、409 来源变化或分页游标失效、422 输入或读取预算不满足、500 存储等内部故障。既有入口保持原有响应合同。

相关测试为 `tests/unit/test_memory_context*.py`、`tests/unit/test_memory_recent_messages.py`、`tests/unit/test_memory_reference_time.py`，以及 `tests/integration/test_memory_authorization.py` 和 `tests/integration/test_memory_task_tenant.py`。集成测试使用隔离资源与当前消息后端，模型边界受控；这些检查不等同真实模型效果或所有存储后端的验收。
