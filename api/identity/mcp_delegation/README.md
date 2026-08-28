# MCP request-scoped delegation

本模块是 EIM-P3 当前实现。它只把服务端已经验证的 `RunContext`、发布 Agent revision、A4
canonical tool policy、MultiRAG grant 与 A2 issuer 组合成一次逻辑 MCP 调用的短期 bearer；不接受
DSL、模型 alias、tool description、静态 header 或调用参数作为授权来源。

## 当前行为

- `identity.mcp_delegation.enabled` 默认是 `false`；关闭时所有既有 MCP server 保持 legacy 行为。
- 启用时 API lifespan 一次性加载 `secure` profile 的 A4 `tool-policies.json`（`snapshot_format` 接受
  2 或 3，当前 of_mcp 产出 3；`profile` 必须逐字是 `secure`）和本模块 format-1 `mcp-grants.json`。
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
- bearer 不进入 Agent/session/global cache、静态 server headers、repr、tool metadata 或稳定错误文案。

Dialog target 当前没有可验证的发布 revision，非 Channel ChatAgent 也没有 P2 Principal/revision；若它们
引用 delegated server，会因上下文不足拒绝，不会回退到 static auth。当前可完成动态委托的既有入口是
带 C3 Principal 与已验证 Canvas release 的 Channel Canvas execution。

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
```

配置、key、policy/grant 制品、API 重启、真实 of_mcp secure endpoint 和真实工具调用必须作为独立 rollout
逐项批准和验证。P3 代码完成不代表 A5、A6 production backend、M1/M2、U14、remote release 或真实
环境迁移已经完成。
