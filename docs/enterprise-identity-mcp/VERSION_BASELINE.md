# 技术版本与上游基线

> **基础版本核验：2026-08-07；飞书 SDK、官方文档和交互参考仓刷新：2026-08-09
> （Asia/Shanghai）**
> 版本会变化。本文记录的是可复现快照和选型规则，不是“永远最新”的承诺。

---

## 1. 当前与目标版本

| 组件 | 当前仓库 | 最近核验的官方最新 | 本项目目标 | 处理方式 |
|---|---|---|---|---|
| Python | MultiRAG `>=3.12,<3.15`；of_mcp `>=3.12` | — | 保持 3.12+ | 不降级 |
| `lark-oapi` | 声明 `>=1.2,<2`，lock 为 1.7.1 | **1.7.2** | `>=1.7.2,<2` | EIM-F1 独立升级 |
| `lark-channel-sdk` | 未安装 | **1.2.0** | `>=1.2.0,<2` | EIM-C5 PoC 通过后才引入 |
| MCP Python SDK `mcp` | MultiRAG 仍走 v1 风格客户端 | **2.0.0 stable** | `>=2.0,<3`，直接使用官方 `Client` | EIM-F2 独立迁移 |
| `mcp-types` | of_mcp 传递依赖 | **2.0.0 stable** | 由 `mcp==2.0.*` 精确匹配 | 不单独 pin，除非只消费 wire types |
| FastMCP stable | MultiRAG lock 3.4.4 | **3.4.6** | 不作为新 MCP 客户端抽象 | 上游需要时单独升级 |
| FastMCP 4 prerelease | of_mcp 固定 4.0.0b1 | **4.0.0b2** | 先兼容验证，再固定 b2 或当时更新版本 | EIM-F3，禁止顺手升级 |
| MCP 协议 | 混合旧客户端/新 server | **2026-07-28** | 新请求走 2026-07-28；迁移期保留 legacy 兼容 | 兼容测试固定 |

版本来源：

- [`lark-channel-sdk` PyPI](https://pypi.org/project/lark-channel-sdk/)
- [`lark-oapi` PyPI](https://pypi.org/project/lark-oapi/)
- [`mcp` PyPI](https://pypi.org/project/mcp/)
- [`fastmcp` PyPI](https://pypi.org/project/fastmcp/)
- [MCP Python SDK v2 What's New](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)
- [MCP 2026-07-28 发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

### 为什么 MultiRAG 新客户端直接依赖 `mcp`，不以 FastMCP Client 为核心

MultiRAG 当前 `common/mcp_tool_call_conn.py` 已经直接使用 MCP SDK 的 `ClientSession`。
SDK v2 提供一等 `Client`，能连接 URL、自动选择现代 `server/discover` 或回退旧
`initialize`，并同时兼容 2026 和 legacy server。新客户端直接使用标准 SDK，可把 MultiRAG
与 of_mcp 的 FastMCP 实现版本解耦。

FastMCP 继续作为 of_mcp 的 server/composition 框架，但授权契约必须基于标准 HTTP/OAuth/MCP，
不得使用只有某个 FastMCP beta 才认识的私有 Header 作为唯一方案。

---

## 2. 官方上游提交快照

以下 SHA 由 `git ls-remote <repo> HEAD` 获取；飞书交互相关仓于 2026-08-09 刷新。后续参考源码时，先用 SHA
重现本文看到的行为，再对比最新 HEAD，避免文档链接随 main 漂移。

| 项目 | 快照 SHA | 用途 |
|---|---|---|
| `larksuite/channel-sdk-python` | `731d459cca55ac76e85911bba2b1666508145e03` | 飞书 Channel SDK、strict security、卡片与去重 |
| `larksuite/oapi-sdk-python` | `8d6402635d0a9314ddae765ae64931aabca30f79` | 通讯录 V3、token 生命周期、完整 OpenAPI |
| `larksuite/openclaw-lark` | `dde0be3680d6fd5443cab426c8f4b3216266346a` | 流式卡片、敏感确认、飞书资源工具和安全警告 |
| `openclaw/openclaw` | `73bdb4b924f6db3c4ab45c5e40fbf61b06fa56a0` | 生产 Feishu channel 能力矩阵、typing/streaming/media/thread policy |
| `bytedance/deer-flow` | `e16ef2969b1446162e19af7bdde1446674851e66` | `channel_connections`、单卡 streaming、follow-up queue、owner 隔离 |
| `langbot-app/LangBot` | `22c389edc16149828380c7153c0b492400f66a5f` | 多 Provider、访问控制、Lark WS/Markdown 与运维面 |
| `shareAI-lab/lark-channel` | `cf056995730a3775529c3bf87fce8033cea554a4` | 群组/线程隔离、工具过程流式卡片 |
| `modelcontextprotocol/python-sdk` | `a4f4ccd091138771535e17191123f20b30fda68e` | MCP SDK v2 客户端、双协议兼容和 OAuth |

源码参考的具体内容和禁止照搬项见 [REFERENCES](REFERENCES.md)。

---

## 3. 每个版本任务开工前的重新核验

只依赖 Python 标准库的 PyPI 查询：

```bash
python3 - <<'PY'
import json
import urllib.request

for package in ("lark-channel-sdk", "lark-oapi", "fastmcp", "mcp", "mcp-types"):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=20) as response:
        data = json.load(response)
    print(package, data["info"]["version"], data["info"].get("requires_python"))
PY
```

注意：PyPI JSON 的 `info.version` 默认给最新**稳定版**，不会显示 FastMCP 4 的最新预发布。
FastMCP 4 必须额外查看 [FastMCP releases](https://pypi.org/project/fastmcp/#history)，或枚举
JSON `releases` 中的 `4.*` 版本。

上游 HEAD：

```bash
git ls-remote https://github.com/larksuite/channel-sdk-python.git HEAD
git ls-remote https://github.com/larksuite/oapi-sdk-python.git HEAD
git ls-remote https://github.com/modelcontextprotocol/python-sdk.git HEAD
git ls-remote https://github.com/modelcontextprotocol/ext-auth.git HEAD
```

每次升级 PR 必须在 ROADMAP 变更日志记录：

- 查询日期；
- 旧版和新版；
- 官方 changelog/release URL；
- 破坏性差异；
- 回滚版本；
- 真实兼容测试结果。

---

## 4. 飞书 SDK 选型规则

### Channel transport

优先用独立 `lark-channel-sdk` 做 Channel transport PoC；它是官方后继方向，不是已经被本项目证明
更稳定的既定替换。只有 [EIM-ADR-11](DECISIONS.md#eim-adr-11飞书传输优先评估官方独立-channel-sdk但不预设迁移成功)
和本文升级闸门全部通过才正式引入：

- `FeishuChannel` 作为 SDK 边界；
- WebSocket 长连接是企业自建应用默认传输；
- Webhook 仅用于 FaaS、集中公网入口或企业运维明确要求；
- 生产从 `SecurityConfig(mode="audit")` 观察，再进入 `strict`；
- MultiRAG 继续拥有队列、Redis 去重、会话、binding、Secret、租户和执行控制。

该 PoC 不阻塞 [EIM-U0/U1](ROADMAP.md#10-phase-u--用户与管理员体验)。现有 `lark-oapi`
OpenAPI 已足以实现 reaction、reply UUID、CardKit 流式更新和媒体资源；UX 先落在稳定
ReplySession/Provider 接口上，transport 后续可替换。

官方文档：

- [Channel SDK README](https://github.com/larksuite/channel-sdk-python)
- [从 lark_oapi.channel 迁移](https://github.com/larksuite/channel-sdk-python/blob/main/docs/migration-from-lark-oapi.md)
- [安全模式](https://github.com/larksuite/channel-sdk-python/blob/main/docs/security.md)

### OpenAPI

保留 `lark-oapi` 负责：

- Contact V3 `GET /contact/v3/users/:user_id`；
- tenant access token 生命周期；
- IM reply/create 的 `uuid`、`reply_in_thread`，消息 reaction、CardKit create/update/finish；
- 消息图片/文件/音视频资源上传下载；
- 用户状态和 employee_no；
- 通讯录 created/updated/deleted/scope events；
- 未来可选的飞书文档、日历等 OpenAPI。

2026-08-09 已复核当前 lock 对应的 `lark-oapi` 1.7.1 tag（commit `2cecb91d`）：
`lark_oapi/api/cardkit/v1/` 已包含 create card、element content update 和 card settings typed request，
`lark_oapi/api/im/v1/` 已包含消息发送/回复模型。因此 EIM-U1 不把 F1/C5 设为硬依赖；F1 仍应作为
独立补丁升级尽早完成，但不能和 U1 混成一个提交。

禁止重新手写 token 刷新、请求签名或完整通讯录 HTTP client；只有为解决 SDK 未覆盖/阻塞行为且有
测试证据时，才允许封装最小 httpx adapter。

---

## 5. MCP 2026-07-28 必须采用的现代语义

- 新协议无 `initialize/initialized` 握手和 `Mcp-Session-Id`；每个请求自包含。
- 新客户端使用 `server/discover`，无法识别时自动回退 legacy。
- HTTP 请求携带 `MCP-Protocol-Version`、`Mcp-Method`，工具请求还携带 `Mcp-Name`。
- 不以连接或进程内 session 保存用户身份。
- MCP server 是 OAuth protected resource，token 必须绑定目标 resource/audience。
- DCR 已走向弃用；通用交互式客户端优先 Client ID Metadata Documents。
- 企业员工统一授权的长期方向是 EMA；纯机器到机器使用 OAuth Client Credentials extension。

官方入口：

- [2026-07-28 规范发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Authorization Extensions](https://modelcontextprotocol.io/extensions/auth/overview)
- [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)

在官方站点尚未把所有导航页切到 `2026-07-28` 路径时，以 2026 发布说明、SDK v2 文档和最终
SEP 为准，不能因此退回旧 session 设计。

---

## 6. 升级闸门

### `lark-oapi`

- 现有飞书 Channel 单元测试全绿；
- Contact V3 的 open_id -> user_id 实测；
- tenant token 缓存和并发刷新测试；
- 无新 import-time event-loop 副作用。

### `lark-channel-sdk`

- SDK 回调在 3 秒内只规范化并入队；
- 消息 ID 去重责任没有与现有 Redis store 冲突；
- 多 worker/leader lease 行为实测；
- `audit` 无未解释告警后才进 `strict`；
- 回滚到现有 transport 不改变上层 `IncomingMessage` 契约。
- 对 `channel.stream()` 的节流、sequence、finish、取消和错误行为做 contract comparison；即使比
  现有 renderer 更方便，也不能把 EIM-U1 重新变成 C5 的依赖。

### MCP SDK 2

- 对 of_mcp 2026 server 走现代模式；
- 对至少一个 legacy fixture 自动回退；
- list/call/tool error、取消、超时和认证 401/403 行为固定；
- 请求级 token 不被跨 Principal 复用；
- 移除旧 `streamablehttp_client`/手动 `initialize` 后无悬挂 task。

### FastMCP 4 beta

- 完整执行 `/Users/xldu/project/of/of_mcp/AGENTS.md` 中的实测修正回归；
- mount/proxy 契约逐字节等价；
- `provider_error_strategy`、FileSystemProvider、依赖注入和 auth middleware 行为复测；
- `uv run ofmcp verify` 全绿；
- 版本升级 PR 不包含任何身份/授权功能变更。
