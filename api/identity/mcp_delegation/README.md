# MCP request-scoped delegation

本模块是 EIM-P3 当前实现。它只把服务端已经验证的 `RunContext`、发布 Agent revision 或独立开发快照、A4
canonical tool policy、MultiRAG grant 与 A2 issuer 组合成一次逻辑 MCP 调用的短期 bearer；不接受
DSL、模型 alias、tool description、静态 header 或调用参数作为授权来源。

## 当前行为

- `identity.mcp_delegation.enabled` 默认是 `false`；关闭时所有既有 MCP server 保持 legacy 行为。
- 启用时 API lifespan 一次性加载 `secure` profile 的 A4 `tool-policies.json`（`snapshot_format` 接受
  2 或 3，当前 of_mcp 产出 3；`profile` 必须逐字是 `secure`）和本模块 format-1/2 `mcp-grants.json`。
  两个文件必须是绝对路径、regular、非 symlink、至多 1 MiB，且在 POSIX 上不能 group/world
  writable。
- 两个 revision 都由移除各自 revision 字段后的 canonical JSON SHA-256 复算。它们用于完整性与
  drift 检测，不替代文件发布权限、制品签名或 O1 rollout 控制。
- delegated binding 是 `MCP server id -> resource name -> exact HTTPS audience` 的一对一映射。
  server 自身 tenant、URL、Streamable HTTP transport、A2 resource audience 必须完全一致；静态
  `Authorization` 与 delegated SSE 都拒绝。
- grant 精确绑定 tenant、platform user、published Agent id/revision 与 resource。模型只能看见当前
  grant/scope/assurance 允许的 canonical tools；模型 alias 只用于去重，wire 与授权始终使用原名。
- 只缓存有界的 immutable grant/scope decision。请求的 ACR、AMR 和 enterprise subject 每次重验；
  A2 当前不签 `amr`，所以任何 `required_amr` 非空的工具都在发网前 fail closed。
- 每次逻辑 `tools/call` 都向 A2 申请新 token/JTI。MCP SDK 2 的 operation-scoped `httpx2.Auth` 在该次
  initialize、call 和 transport retry 中复用同一 bearer，调用结束立即关闭 client；下一次调用重新签发。
- `identity.mcp_delegation.tls_ca_bundle_file` 可为 delegated Streamable HTTP 配置一份绝对路径的
  CA bundle。启用后只给上述 operation-local client 注入独立 `SSLContext`，不读取全局
  `SSL_CERT_FILE`、不关闭证书校验，也不改变 legacy/static MCP 连接；未配置时保留 SDK 的系统信任。
- bearer 不进入 Agent/session/global cache、静态 server headers、repr、tool metadata 或稳定错误文案。

Dialog target 当前没有可验证的发布 revision，非 Channel ChatAgent 也没有 P2 Principal/revision；若它们
引用 delegated server，会因上下文不足拒绝，不会回退到 static auth。已接入动态委托的入口包括
带 C3 Principal 与已验证 Canvas release 的 Channel Canvas execution，以及下述 Web Canvas 运行。

Web `/api/v1/agents/chat/completion` 的草稿调试、发布运行、会话续聊与 OpenAI 兼容分支现在均由
服务端传入可信上下文。客户端的 `run_context`、`prepared_run` 不进入授权；`user_id` 只能作为
既有 replica/展示标签，不能替换当前 Principal。团队发布运行先检查当前有效成员关系，再把同一
调用者的 Web/API 认证证据绑定到资源 tenant，不使用 Agent 创建者的凭据。

- 新 Web 会话与 `t_ai_agent_execution_origins` 在同一事务内创建。来源包含运行用户、tenant、Agent、
  模式、精确发布 revision（草稿为空）、完整配置快照及 SHA-256。标题继续作为展示字段。
- 续聊重查访问权限、来源用户和发布版本可用性；执行配置取原始快照，只恢复服务端已存储的会话
  状态和组件输入/输出值。修改草稿、切换发布版本或会话运行 DSL 不替换原始授权目标。
- 旧会话没有可靠来源时继续使用既有普通工具；引用 delegated server 时拒绝，不按标题、DSL 或
  最新发布版本推断权限。需要委托的用户应新建会话。
- 独立创建会话接口继续只允许 owner。团队成员仍通过发布 completion 建立自己的会话；其来源
  绑定实际运行者。草稿委托仅允许当前 owner 编辑调试，并且必须有独立 development grant。
- Web 发布和草稿委托都只开放 `effect=read` 且 `replay_mode=reusable` 的工具。Web 敏感确认和
  MRTR 暂停/恢复尚未接入；Channel 的发布版本 interaction 合同保持原样。
- 初始化阶段的预期授权拒绝在 SSE headers 前返回 HTTP 403，保留非零 `retcode`、`data=false`、
  稳定 `error_code` 与 `Cache-Control: no-store`；制品或签发故障返回 503，不吞异常或回退静态凭据。
- 每次签发的 current OTel span 可记录运行模式、policy/grant revision 和草稿配置摘要；没有配置
  exporter 时为 no-op。不记录身份、参数、结果或 bearer。

这项 Web/开发扩展的授权边界见 [EIM-ADR-28](../../../docs/enterprise-identity-mcp/DECISIONS.md#eim-adr-28web-发布运行与只读草稿开发授权分离)。

## Authority artifacts

`tool-policies.json` 由 of_mcp 的 canonical contract builder 生成；MultiRAG 只消费经过评审的 secure
snapshot，不在运行时从 `tools/list` 反推 policy。当前 of_mcp checkout 中若只有 `profile=local` 的
snapshot，它不能用于启用 P3。

`mcp-grants.json` 的形状如下。revision 值必须由发布工具计算，下面的占位符不能直接使用：

```json
{
  "snapshot_format": 1,
  "policy_revision": "<exact A4 policy_revision>",
  "grant_revision": "<canonical SHA-256 without this field>",
  "credential_generation": 1,
  "bindings": [
    {
      "mcp_server_id": "<server id>",
      "resource_name": "ofmcp_gateway",
      "audience": "https://gateway.example/mcp"
    }
  ],
  "grants": [
    {
      "tenant_id": "<tenant id>",
      "platform_user_id": "<platform user id>",
      "agent_id": "<published Canvas agent id>",
      "agent_revision_id": "<published release id>",
      "resource_name": "ofmcp_gateway",
      "allowed_scopes": ["leave:read"]
    }
  ]
}
```

需要开放草稿调试时，将 grant 制品升级为 `snapshot_format: 2`，保留 `grants` 的发布版本语义，
另加 `development_grants` 数组。只有开发授权时 `grants` 可为空，但两类 grant 合计不得为空。
format 1 的形状和行为保持兼容；format 2 不接受在 development grant 中填写发布 revision。

```json
{
  "tenant_id": "<Agent owner tenant>",
  "platform_user_id": "<actual developer>",
  "agent_id": "<editable Agent id>",
  "resource_name": "ofmcp_gateway",
  "allowed_scopes": ["leave:read"],
  "allowed_tools": ["leave_get_balance"]
}
```

上述对象是 `development_grants` 中的一项。工具名必须是 A4 canonical name，并且逐项满足
`read/reusable`、scope 完整覆盖；重复、未知工具、prepare、side_effect 和未知 scope 都在加载时
拒绝。需要重新计算整个文件的 `grant_revision`，不能只手改 grant 而沿用旧摘要。

数据库迁移新增 `b0d2e4f6a8c0`。升级现有数据库后才部署新 API；来源表以 session id 为主键和
cascade 外键，删除会话时同步删除来源。有来源材料时 downgrade 拒绝丢弃，需先完成明确的数据
迁移/保留方案。不会回填猜测的历史发布版本。迁移操作见 [数据库迁移指南](../../../docs/database-migration.md)。

启用配置属于部署动作，不能直接写入仓库的 `configs/service_conf.yaml`：

```yaml
identity:
  mcp_issuer:
    enabled: true
    # 其余 A2 authority/key 配置见 enterprise-identity-mcp 契约
  mcp_delegation:
    enabled: true
    tool_policy_file: /etc/multirag/tool-policies.json
    grant_policy_file: /etc/multirag/mcp-grants.json
    tls_ca_bundle_file: /etc/multirag/pki/ofmcp-ca.pem
```

配置、key、policy/grant 制品、API 重启、真实 of_mcp secure endpoint 和真实工具调用必须作为独立 rollout
逐项批准和验证。P3 代码完成不代表 A5、A6 production backend、M1/M2、U14、remote release 或真实
环境迁移已经完成。
