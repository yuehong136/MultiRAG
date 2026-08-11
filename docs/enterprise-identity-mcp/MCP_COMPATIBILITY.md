# EIM-F2 MCP 兼容矩阵与迁移清单

> 状态：2026-08-12 首轮基线已完成。本文记录可重复的活体协议证据，不替代
> [ROADMAP](ROADMAP.md) 的任务状态、[VERSION_BASELINE](VERSION_BASELINE.md) 的版本选择或
> [MCP_ENTERPRISE_PLATFORM](MCP_ENTERPRISE_PLATFORM.md) 的目标架构。
>
> 本文中的 token 全是公开、低熵、仅 loopback fixture 使用的测试值；没有生产 Secret。

---

## 1. 为什么先做这份矩阵

MultiRAG 当前同时包含：

- outbound MCP SDK 1 Client：`common/mcp_tool_call_conn.py`；
- inbound FastMCP 3 Server：`mcp/server/server.py`；
- 根环境中的 `fastmcp-slim 3.4.4` 要求 `mcp>=1.24,<2`。

所以不能把根环境直接改成 `mcp>=2`，再把“能启动”当成升级完成。EIM-F2 先回答四个问题：

1. 当前 Client 对 legacy 和 modern dual-era Server 实际协商到哪个协议；
2. SDK 2 Client 对当前 MultiRAG Server 是否真的走 modern，还是回退到 initialize；
3. 401/403、tool error、timeout/cancel 是否保留了可恢复语义；
4. MCP 1 与 MCP 2 能否在不污染生产 lock 的独立解释器中重复验证。

F2 不改变生产行为。它发现的缺口进入 F6/F7/F3/F8，不在测试中用 skip、xfail 或宽松断言掩盖。

---

## 2. 已核验环境

| 项目 | 代码/版本 | 锁定证据 |
|---|---|---|
| MultiRAG baseline commit | `2d3836e8f72ff3b40fc7169084dcbdd4ab048100` | 本次工作树在其上增加文档与 F2 fixture |
| MultiRAG root | FastMCP `3.4.4`、fastmcp-slim `3.4.4`、MCP SDK `1.28.1` | `uv.lock` SHA-256 `d8afd4764f16aac2d4c21f51557b6064375e53bfd957ca87885d368093706c00` |
| isolated modern fixture | MCP SDK `2.0.0` | PEP 723 script lock SHA-256 `84b2c9bb83231bec210b4a7b8a49a789637cacd2f4a1d8d9e62375de36d40907` |
| of_mcp baseline commit | `ffa08853cfcb1424ddbec1858edf47fb2a48d366` | 本次工作树只增加 F2 测试基建 |
| of_mcp | FastMCP `4.0.0b1`、MCP SDK/mcp-types `2.0.0` | `uv.lock` SHA-256 `1c29c955b40f984dcc592eb79c727f1d348ca694a9036ef9ff8622fd783372fe` |

版本事实会漂移。重跑时先执行 VERSION_BASELINE 的查询命令；不要把这里的 SHA 当成未来升级目标。

---

## 3. 测试结构

### 3.1 MultiRAG 仓

| 文件 | 责任 |
|---|---|
| `tests/unit/test_mcp_tool_call_conn_compat.py` | 不走网络，固定当前 transport/header/manual initialize、401/403 文本化、tool error metadata、timeout 队头阻塞和 processor cancellation |
| `tests/compat/mcp/legacy_server.py` | 使用根环境 FastMCP 3/MCP 1，提供 legacy HTTP/SSE、structured echo、tool error 和 wait/status |
| `tests/compat/mcp/multirag_server.py` | 启动真实 `mcp/server/server.py` 组装面，随机 loopback 端口，供 SDK 2 验证 inbound 当前协议 |
| `tests/compat/mcp/modern_server.py` | PEP 723 独立 MCP SDK 2 Server/Client；提供 modern/legacy dual-era、路由 Header、401/403、error、wait/status |
| `tests/compat/mcp/modern_server.py.lock` | 现代 fixture 的独立、确定性依赖锁；不进入根 lock |
| `tests/compat/mcp/current_client_probe.py` | 每次只在独立子进程运行一个真实 `MCPToolCallSession` 场景，避免失败连接的旧 event loop 污染矩阵进程 |
| `scripts/check_mcp_compat.py` | 拉起随机端口子进程、读取 ready JSON、执行矩阵、输出 Markdown/JSON、finally terminate/kill 并检查退出 |

### 3.2 of_mcp 仓

| 文件 | 责任 |
|---|---|
| `packages/ofmcp-testing/src/ofmcp/testing/compat_server.py` | FastMCP 4 b1 loopback/stateless 双时代 fixture，含 valid/invalid/insufficient token、结构化结果、ToolError、wait/cancel/status |
| `packages/ofmcp-testing/tests/test_compat_server.py` | modern/legacy、401/403、tool error、等待和受控取消回归 |
| `tests/equivalence/test_mount_vs_proxy.py` | mount/proxy 在 modern/legacy 两种 mode 下比较完整 `CallToolResult` |

两个仓只通过 HTTP/协议交互。MultiRAG 测试不 import sibling `of_mcp`，也不把本机绝对路径写入默认
门禁。

---

## 4. 2026-08-12 活体结果

默认自包含命令：

```bash
cd /Users/xldu/project/multirag
make mcp-compat
```

结果：**12/12 PASS**；全部 fixture 使用随机 loopback 端口，命令退出后未发现残留 server 进程。

| ID | Client | Server | 实际协议 | 结果与含义 |
|---|---|---|---|---|
| `legacy-sse-success` | MultiRAG SDK 1 | FastMCP 3 fixture | legacy initialize/SSE | 结构化 echo 成功；SSE 只保留迁移冒烟，不扩成全矩阵 |
| `modern-client-to-current-multirag` | SDK 2 `mode=auto` | 真实 MultiRAG FastMCP 3 | `2025-11-25` | SDK 2 先 probe，再回退 initialize；当前 inbound Server **不是** modern |
| `legacy-http-success` | MultiRAG SDK 1 | FastMCP 3 fixture | legacy initialize/HTTP | 结构化 echo 成功 |
| `modern-self-probe` | SDK 2 `mode=2026-07-28` | MCPServer 2 | `2026-07-28` | 收到 `Mcp-Method=tools/call`、`Mcp-Name=compat_echo`，无 session ID |
| `legacy-client-to-modern-server` | MultiRAG SDK 1 | MCPServer 2 dual-era | `2025-11-25` | 工具成功但明确是 legacy fallback，不能误报为 MultiRAG 已支持 modern |
| `modern-client-auto-fallback` | SDK 2 `mode=auto` | FastMCP 3 legacy-only | `2025-11-25` | 证明 SDK 2 的 discover -> initialize 回退路径 |
| `auth-valid` | MultiRAG SDK 1 | protected MCPServer 2 | `2025-11-25` | fixture bearer 可通过，报告不输出凭据 |
| `auth-401` | MultiRAG SDK 1 | protected MCPServer 2 | HTTP 401 | fixture 的确返回 401，但当前 Client 对外只给普通 connection error |
| `auth-403` | MultiRAG SDK 1 | protected MCPServer 2 | HTTP 403 | fixture 的确返回 403，但当前 Client 不能与 401 做 typed 区分 |
| `tool-error` | MultiRAG SDK 1 | MCPServer 2 | `2025-11-25` | 主返回被压成文本，`is_error=true` 只留在旁路 metadata |
| `timeout-does-not-cancel` | MultiRAG SDK 1 | MCPServer 2 | `2025-11-25` | 调用方超时后 handler 仍完成；串行 worker 在此期间继续被占用 |
| `modern-caller-cancel` | SDK 2 modern | MCPServer 2 | `2026-07-28` | caller task 已取消，但 server 状态从 `running` 到 `completed`；不能把 transport cancel 当业务回滚 |

矩阵的 PASS 表示“预期现状已被可靠观察”，不表示现状已经满足目标。例如 401/403 折叠和超时不取消
是通过测试固定的迁移缺口，不是被认可的生产终态。

---

## 5. 两仓真实交叉验证

终端 A：

```bash
cd /Users/xldu/project/of/of_mcp
uv run --locked python -m ofmcp.testing.compat_server \
  --host 127.0.0.1 --port 18765
```

终端 B 使用 MultiRAG 当前真实 Client，URL 为 `http://127.0.0.1:18765/mcp`，测试 bearer 为
`ofmcp-eim-f2-valid`。2026-08-12 实测 `compat_echo` 返回：

```json
{
  "value": "multirag-cross-repo",
  "protocol_version": "2025-11-25"
}
```

这再次证明：of_mcp 虽然运行 MCP SDK 2/FastMCP 4，MultiRAG 当前 Client 与它协商的仍是 legacy
era。不能仅凭“调用成功”宣布 SDK 2/modern 升级完成。

of_mcp 自身验证：

```text
compat fixture tests: 14 passed
uv run --locked ofmcp verify: 116 passed, 2 skipped; six gates passed
```

其中 FastMCP 4 b1 的 `StaticTokenVerifier` 会把已知但缺 scope 的 token 提前折叠为 401；测试 fixture
使用专用 verifier/middleware 才能稳定产生 HTTP 403。这个适配只存在测试包，没有修改 gateway 或
领域服务。

---

## 6. 当前缺口与迁移要求

### 6.1 协议与 API

- outbound Client 手调 `initialize()`，不能发 modern self-describing request；
- inbound MultiRAG Server 对 SDK 2 `auto` 只协商到 `2025-11-25`；
- 当前成功路径会把 `structuredContent` 再序列化为模型字符串，typed 结果只在旁路 metadata；
- 当前 Client 不支持 `InputRequiredResult/inputResponses/requestState`。

### 6.2 错误与取消

- 401/403 在真实 transport 的异常组中折叠，调用方无法安全决定登录、step-up 或永久拒绝；
- 初始化事件可能在底层连接错误完成归类前被置位，矩阵中 401/403 的 `initial_ready` 观察为 `true`；
- `tool error` 主路径是文本，错误 taxonomy 不稳定；
- `_call_mcp_server()` timeout 只取消 `results.get()`，不取消 `ClientSession.call_tool()`；
- 一个 server 的后台 worker 串行处理任务，慢调用继续造成 head-of-line blocking；
- modern caller task cancellation 也没有自动形成服务端业务取消；Action/Task/OA 状态必须另建契约。

### 6.3 依赖与发布

- FastMCP 3 `mcp<2` 与新 Client 的 `mcp>=2` 不能在根环境直接共存；
- MultiRAG 顶层源码目录也叫 `mcp/`，拆 runtime 时必须审计包名和 `sys.path`，避免遮蔽官方包；
- of_mcp FastMCP 4 仍是 beta，b1 -> b2 必须留在 F4 纯版本任务；
- F2 的 PEP 723 环境是测试隔离，不是生产部署方案。

---

## 7. 后续任务的精确清单

### EIM-F6：选择依赖拓扑

1. 在干净 resolver 中重现 `fastmcp-slim 3.4.4 -> mcp<2` 冲突；
2. 比较独立 `mcp/server` project/venv/lock、经批准同步升 FastMCP 4 beta、等待 stable 三案；
3. 审计顶层 `mcp/` 与官方 `mcp` 包同名；
4. 固定进程、启动、健康、CI、部署、lock owner 和回滚；
5. 默认推荐独立 Server runtime，除非证据证明同环境 beta 升级风险更低。

### EIM-F7：实施运行时边界

- 只实施 F6 选定的依赖/进程边界；
- 根和 Server 环境分别 cold install、lock check、start、health、rollback；
- 不改生产 Client API，不加身份/授权功能。

### EIM-F3：outbound Client v2

- 使用官方 `mcp.Client(mode="auto")`，不再手调 initialize；
- 每次逻辑调用从 request-scoped credential provider 获取 bearer；
- typed 保留 structured result、tool error、401、403 和 negotiated protocol；
- timeout/caller cancel 必须终止底层调用或把未知执行状态显式上报，不能继续阻塞串行队列；
- 支持 `InputRequiredResult`，但持久化/UI 属于 U14/U15；
- 完整重跑本文两方向矩阵。

### EIM-F8：inbound modern Server

- 单独迁移 `mcp/server/server.py` 到 MCP `2026-07-28`；
- modern probe 必须看到 `server/discover`、routing headers 和无 session 调用；
- legacy SDK 1 仍按门禁回退；
- `/health`、structured output、Host/Origin 防护和现有只读工具不回归；
- Principal/scope/protected-resource metadata 留给 A7，不混入协议任务。

### EIM-A7：inbound Resource Server auth

- 为 MultiRAG MCP 定义独立 resource URI、audience、scope namespace 和 protected-resource metadata；
- 不能接受或转发 of_mcp bearer；
- `tools/list` 可见性和 direct call 同时授权；
- dataset/tenant 业务授权保留在 MultiRAG；
- legacy API key 退出必须有真实调用量和回滚门禁。

---

## 8. 重新运行与停止条件

默认封闭验证：

```bash
cd /Users/xldu/project/multirag
uv run --no-sync pytest -q tests/unit/test_mcp_tool_call_conn_compat.py
make mcp-compat
make verify
```

of_mcp：

```bash
cd /Users/xldu/project/of/of_mcp
uv run --locked pytest -q packages/ofmcp-testing/tests/test_compat_server.py \
  tests/equivalence/test_mount_vs_proxy.py
uv run --locked ofmcp verify
```

完成命令后还要检查没有残留 `legacy_server.py`、`modern_server.py`、`multirag_server.py` 或
`compat_server.py` 进程。

遇到以下任一情况停止，不启动 F3/F8：

- 根 `pyproject.toml`/`uv.lock` 因 F2 发生变化；
- modern self-probe 没有明确得到 `2026-07-28`；
- SDK 2 -> 当前 MultiRAG 的成功没有证明是 legacy fallback；
- 401/403 fixture 自己不能稳定产生两个状态；
- fixture 依赖 sibling import、固定端口、真实 Secret、外网服务或 skip/xfail；
- 子进程不能有界停止或遗留端口；
- resolver 只能通过未批准的 prerelease/major 变化求解。

这份结果回答的是“现在的边界是什么”。它不授权生产依赖升级、FastMCP 4 beta 迁移、飞书后台变更、
真实 OA/Jira 调用或删除 legacy endpoint。
