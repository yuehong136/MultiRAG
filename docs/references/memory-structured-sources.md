# 结构化来源与提交凭据

结构化来源是可选的 RAW 写入入口，保留调用者提供的分段角色、事件时间和服务端提交凭据。
它复用既有追加、幂等操作、来源版本、容量和清理流程，不新增消息类型或独立存储。
原 `/messages` 的合并正文和自动提取行为保持原样；旧 RAW 不按正文标签或 JSON 外形追认角色。

## 显式提交

先读取 `GET /api/v1/memories/{memory_id}/capabilities` 的 `structured_sources`。

```http
POST /api/v1/memories/{memory_id}/sources
Content-Type: application/json

{
  "operation_id": "record-project-discussion",
  "user_id": "external-subject",
  "agent_id": "assistant",
  "session_id": "discussion",
  "segments": [
    {"role": "user", "content": "仅离线评估时使用简短中文。"},
    {"role": "assistant", "content": "这是助手的回复，不能替代用户确认。"}
  ],
  "event_time": "2026-11-16T14:30:00+08:00"
}
```

允许 1–32 段，角色为 `user`、`assistant`、`tool` 或 `unknown`；正文不能只有空白。
完整规范 JSON 不超过 65,535 UTF-8 字节，继续受已有追加 token／容量限制约束。
主体、Agent、会话各不超过 256 UTF-8 字节，空字符串保持精确空值。未知字段及自报可信状态被拒绝。
JWT 的 `user_id` 由认证用户替换；SDK token 可以明确提供外部主体，但不能借此取得其他 Memory 权限。

此入口只保存 RAW，不隐式调用聊天模型、创建提取任务、审核或发布。
整理时显式选择来源，复用[Dreaming](memory-maintenance.md)或[增量整理](memory-incremental.md)。
尚未启用受管修订时，仍须按既有流程显式登记并启用受管约束。

成功返回既有追加回执，包括十进制字符串 `raw_source_id`、索引状态和 `queue_state=not_required`。
相同操作和相同规范输入重放复用来源、首个接收时间及已完成的 embedding。
修改分段、角色、事件时间或范围后复用操作 ID 返回冲突；事件时间的 UTC 偏移保留为声明的一部分。

## 时间、角色和证明范围

| 信息 | 来源与含义 |
| --- | --- |
| `received_at` | 服务端首次受理时保存的 UTC 时间；重试不换成当前时刻 |
| `event_time`、分段 `role` | 调用者声明；事件时间须带显式时区，可省略或为 null，未知时不补成“现在” |
| `provenance.submitted_by` | 认证提交者；证明该身份提交了数据，不证明分段说话者身份或正文事实 |

既有 `valid_at` 继续作为接收参考时间参与排序与提取，不被客户端事件时间覆盖。
`speaker_identity_verified=false`、`content_verified=false` 不会因角色为 user/tool 或正文自称检查通过而升级。
嵌入指令、工具自述、角色、时间与来源哈希均为数据，不能改变权限、工具规则或人工审核要求。

## 按需查看

```http
GET /api/v1/memories/{memory_id}/sources/{source_id}/provenance
```

默认只返回有界元数据：格式、角色声明类型、段数、角色集合、事件时间、接收时间、提交凭据、来源版本／代次、范围和哈希。
`segments=null`，不自动下载完整分段正文。明确指定 `include_segments=true` 才返回完整分段，仍检查相同权限与来源状态。
普通原文查看继续使用[分段摘录](memory-query.md#分段读取原始来源)；Web 在已有来源面板中按需展示元数据，正文继续沿用分页摘录。

绑定已观察版本时，同时提供 `expected_source_revision`、`expected_source_generation`、`content_sha256` 及完整 `user_id`、`agent_id`、`session_id`。
可另传 `expected_provenance_sha256`，绑定服务端凭据。读取前后重新检查权限、来源和凭据，中间读取真实消息时不持 SQL 事务。
返回 `Cache-Control: no-store`；变化为 409，撤回、不可用或无权为 404，不能用旧元数据补上失败的正文读取。

旧 RAW 返回 `format=legacy_combined`、`role_status=role_unknown`，无结构化分段或推测的接收时间。
看似结构化的旧 JSON 也保留旧语义。仅当服务端写入标记和匹配的持久凭据共同成立时才解析结构化来源；缺失或损坏凭据拒绝读取。

## 整理、核验与清理

Dreaming 快照保留一次完整业务正文和最小提交凭据，完整包装计入输入预算。
超长来源沿用已有分段覆盖，不把片段误称为完整结构化会话。
正文、角色和事件时间由来源版本与内容哈希绑定；提交凭据另有摘要，在分析后及发布前复核。
候选仍为 `hypothesis` / `unverified`，必须人工审核并显式发布。
本次输入合同与分析策略会更新增量整理的策略指纹；绑定旧策略的未完成增量计划须重新预览和创建，
不能把旧策略覆盖记录视为新策略已经分析。既有已发布记忆及旧 RAW 正文不作迁移或回写。

发布依赖、[人工核验](memory-verification.md)及[启动上下文](memory-startup.md)继续绑定提交凭据。
凭据变化后旧候选不能发布，旧确认不能继续供给启动包；旧来源的绑定格式保持兼容。
永久撤回及整库删除复用既有清理，清除 outbox 完整来源／凭据、候选与输入快照等受管副本。
最小审计可保留哈希和状态，不据此宣称已物理清除外部副本或备份。

固定回归与真实模型验证边界见[基线评估](memory-evaluation.md)。定时触发后置、Wiki 等主线；本能力不依赖上层应用。
