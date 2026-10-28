# Outlook / Microsoft 365 邮件

`source: "outlook"` 使用 Graph app-only 凭据，应用需要管理员同意的 `Mail.Read`。
保存配置只验证形状，不访问微软。示例：

```json
{
  "user_ids": ["support@example.com"],
  "folder": "inbox",
  "batch_size": 100,
  "sync_deleted_files": false,
  "credentials": {
    "tenant_id": "directory-id",
    "client_id": "application-id",
    "client_secret": "secret-value"
  }
}
```

`user_ids` 必须是显式的、唯一且非空的字符串数组；不接收 CSV、null 或省略。
`[]` 表示没有邮箱，worker 零认证、零 Graph 请求；绝不扩大为整个租户。
`folder` 是单个 Graph 文件夹 ID 或 `inbox` 等标准名称；只有省略才默认 `inbox`，
显式 null、空串和前后空格均拒绝。邮箱、文件夹和消息 ID 在路径中编码。
`batch_size` / `sync_batch_size` 必须是正整数，后者优先；省略沿用连接器默认值，
不把字符串、布尔或零转为默认值。

既有 `POST /api/v1/connectors/{id}/test` 先校验租户所有权，再读取每个指定邮箱和
文件夹的一条 messages 清单，验证邮件读取边界；不依赖 `/users` 目录查询。
空邮箱范围返回业务错误，不能作为凭据成功的证据。probe 有 30 秒守卫，不写 SQL、对象、
队列或同步状态。Graph/MSAL 复用[有限传输](microsoft_graph.py)，响应头、正文和 token
请求均受截止时间及取消约束；错误不返回供应商响应正文。

每个邮箱完整遍历文件夹 delta 分页到最终 deltaLink，再按同 ID 最后状态归并，删除标记
不入库。分页循环、跨邮箱/文件夹游标、歧义或缺失终态、坏消息和权限错误均失败。
单页沿用 Graph 100 MiB 上限，每个邮箱最多 10,000 页、100,000 个不同 ID。
消息 ID 由编码后的邮箱 ID 和消息 ID 组成，避免跨邮箱同名碰撞。正文保留 HTML，纯文本与
邮件头转义为 HTML，扩展名与实际字节一致；附件只保留存在标记，不下载二进制。
源权限不赋予本地知识库权限。

worker 每轮完整枚举范围，以 `lastModifiedDateTime` 应用 `(poll_range_start, 本轮结束时间]`
窗口；旧邮件的新修改仍可入库。重建从时间零开始。SQL 仅保存现有时间水位，**未持久化跨任务
的 deltaLink**，不宣称只读取本轮增量。直接调用 connector 的消费者可以保存绑定邮箱集和
文件夹的 checkpoint；生成器返回新值，不修改输入，调用方应在入库成功后持久化返回值。
已有 deltaLink 的直接恢复不按旧邮件时间丢弃变更。超过窗口结束的修改拒绝推进水位。

`sync_deleted_files` 只允许 false；Web 隐藏该开关。worker 不注册 Outlook 删除能力，
`retrieve_all_slim_docs_perm_sync` 也明确拒绝，防止变更清单被误作权威库存。空清单、失败、
取消和超时不触发删除或成功水位；受控 HTTP 验收与真实微软租户/管理员同意验收分别报告。
