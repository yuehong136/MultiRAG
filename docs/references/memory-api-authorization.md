# Memory HTTP 权限

`/api/v1/memories` 与 `/api/v1/messages` 使用服务器认证得到的用户 ID，
请求中的 `tenant_id`、`owner_ids`、消息 `user_id` 均不能授予 Memory 权限。

- 个人租户（Memory 的 `tenant_id == 当前用户 ID`）中的 Memory 对所有者可见。
- 其他租户的 Memory 仅在 `permissions=team` 且用户存在当前有效
  owner/admin/normal 成员关系时可见。`me`、邀请、停用及未加入租户均拒绝。
- 配置读取/更新、删除、消息列表/内容读取、遗忘和状态更新在访问存储前检查权限；
  未授权与不存在使用同一 not-found 响应。
- 列表的 `tenant_id` 和 `owner_ids` 同时存在时取交集，与当前可见租户再次取交集；
  显式空筛选返回空列表。计数和分页来自同一个已授权的 SQL 查询。
- 批量消息写入先检查全部 ID，任意缺失或未授权则整体拒绝，不生成消息 ID、
  不存消息、不创建 Task、不入队；重复 ID 仅写一次。此保证针对授权拒绝，
  后续模型、存储或队列运行故障仍沿用既有部分失败报告，不提供跨存储原子事务。
- 批量最近消息和搜索仅保留可见 Memory；全部不可见时返回空结果，不调用消息存储
  或 embedding。消息 `user_id` 是该 HTTP API 的内容筛选/元数据，不能替代用户认证；
  已有 RunContext 通道仍沿用自己的 tenant/principal 绑定。
- 模型更新使用 Memory 所有者租户解析 `llm_id`/`embd_id`；单独提供
  `tenant_llm_id`/`tenant_embd_id` 拒绝，同时提供时重新解析，忽略客户端行 ID。
  提取、embedding 和搜索按 ID 加载模型时也必须匹配 Memory 所有者租户。

测试入口：`tests/integration/test_memory_authorization.py` 使用注册路由、真实 JWT、
隔离 PostgreSQL、当前消息后端与自有 Redis key/stream，模型边界用固定 embedding。
授权拒绝前后独立读回 SQL、消息、缓存、ID 计数及队列；用后检查所建资源清理。
它不证明生产部署、真实模型质量、完整提取 worker 或其他消息后端的运行行为。
