# MCP 2026 兼容矩阵与迁移后运行基线

> 状态：EIM-F2/F3/F4/F6/F7/F8/F9 已完成后的可执行事实。
> 最近验证：2026-08-22（Asia/Shanghai）。
> 长期架构见 [MCP_ENTERPRISE_PLATFORM](MCP_ENTERPRISE_PLATFORM.md)，版本来源见
> [VERSION_BASELINE](VERSION_BASELINE.md)，任务状态只以 [ROADMAP](ROADMAP.md) 为准。

本文既保留升级前 characterization 的结论，也记录升级后的生产运行面。阅读时必须区分：

1. **历史基线**：证明旧 SDK1/FastMCP3 的真实限制，避免迁移后把旧问题忘掉；
2. **当前基线**：MultiRAG 已采用 MCP SDK2/FastMCP4，modern 是主路径；
3. **目标能力**：Principal、OAuth scope、持久交互和飞书表单仍是后续任务，协议升级没有自动完成它们。

---

## 1. 当前结论

截至本次提交：

- MultiRAG outbound 使用官方 `mcp.client.Client` 2.0.0；
- Streamable HTTP 使用 `mode="auto"`，会先 `server/discover`，必要时回退 legacy `initialize`；
- SSE 明确使用 `mode="legacy"`，它不会由 `auto` 自动切换 transport；
- MultiRAG inbound 使用 FastMCP 4.0.0b2，真实协商 MCP `2026-07-28`，同时接受 SDK 2 官方
  registry 中四个 handshake revision：`2024-11-05`、`2025-03-26`、`2025-06-18`、
  `2025-11-25`；
- of_mcp 已在提交 `23dd1fd` 固定 FastMCP 4.0.0b2、MCP/mcp-types 2.0.0；
- 生产主环境不再安装 FastMCP3/MCP1，但测试用 PEP 723 独立锁固定 FastMCP 3.4.7，继续证明
  legacy fallback；
- `make mcp-compat` 的每一格都跑真实 loopback 子进程，不以 mock 或“工具调用成功”替代 wire 证据；
- 协议版本是官方枚举，不按日期字符串猜新旧。SDK registry 漂移会先使单元测试失败；未知 revision
  必须结构化拒绝，不能静默降级成 legacy。

这不代表：

- 当前飞书用户已经成为 MCP Principal；
- 静态 server header 已经变成 request-scoped delegated token；
- inbound/outbound Resource Server 已有标准 OAuth metadata、audience 或 scope；
- `InputRequiredResult` 已有数据库持久化、跨进程恢复或飞书 Form renderer；
- timeout 一定能终止远端业务 handler。

---

## 2. 精确版本与锁

| 运行单元 | 版本 | 锁/提交 |
|---|---|---|
| MultiRAG root | FastMCP/slim `4.0.0b2`、MCP/mcp-types `2.0.0`、httpx2 `2.10.0`、sse-starlette `3.4.8` | `uv.lock` SHA-256 `9d66f20bb3f91f99e99b20e17248699cfbc8ad2e2b5a3d2cfa3ae957c45c2323` |
| legacy oracle | FastMCP `3.4.7` 及其 MCP1 依赖 | `tests/compat/mcp/legacy_server.py.lock` SHA-256 `deadb04931edf60400b23544d7be3f1a94c2fd570062d35b92d6db4da23b96fc` |
| modern oracle | MCP SDK `2.0.0` | `tests/compat/mcp/modern_server.py.lock` SHA-256 `84b2c9bb83231bec210b4a7b8a49a789637cacd2f4a1d8d9e62375de36d40907` |
| of_mcp | FastMCP/slim `4.0.0b2`、MCP/mcp-types `2.0.0` | EIM-F4 `23dd1fd`；lock SHA-256 `9ceecde115825a75320a7877cf5c203ff731d594645aaae629014f6d3c3ca8ce` |

FastMCP 4 仍是 beta，因此 MultiRAG 同时：

- 直接依赖 `fastmcp==4.0.0b2`；
- 直接依赖 `mcp==2.0.0`，因为业务代码直接使用官方 Client；
- 直接依赖 `httpx2>=2.10.0,<3.0.0`，因为业务代码拥有并管理 HTTP transport client；
- 在 uv constraints 固定 `fastmcp-slim==4.0.0b2`，防止 wrapper/slim 漂到不同 beta；
- 不直接依赖 `mcp-types`，代码继续从 `mcp.types` 导入；
- 把后续 b3/RC/GA 当作新任务，不使用开放的 `>=4` 静默升级。

### 顶层目录同名边界

仓库有本地 `mcp/` 目录，官方依赖也叫 `mcp`。当前安全成立的原因是本地目录**没有
`__init__.py`**，而 site-packages 中官方 `mcp` 是 regular package。

必须保持：

```bash
uv run mcp/server/server.py ...
```

不得改成：

```bash
python -m mcp.server.server
```

也不得为了“包化”给本地目录添加 `__init__.py`，否则会遮蔽官方 SDK。

---

## 3. Fixture 与职责

| 文件 | 职责 |
|---|---|
| `tests/compat/mcp/legacy_server.py` | PEP 723 FastMCP3 legacy-only HTTP/SSE；echo、tool error、wait/status |
| `tests/compat/mcp/legacy_server.py.lock` | 真实旧时代 oracle 的独立锁，不污染 root |
| `tests/compat/mcp/modern_server.py` | PEP 723 MCPServer2 dual-era、四个 typed handshake counteroffer oracle、未知版本/protected endpoint、routing-header capture、SDK2 probe |
| `tests/compat/mcp/modern_server.py.lock` | modern oracle 独立锁 |
| `tests/compat/mcp/current_client_probe.py` | 从 MultiRAG root 调真实生产 `MCPToolCallSession` |
| `tests/compat/mcp/multirag_server.py` | 启动真实 inbound Server，隔离后端 API，并只暴露协议路由 header 证据 |
| `scripts/check_mcp_compat.py` | 分配随机 loopback 端口、启动/回收全部 server oracle、执行 22 格矩阵、输出 Markdown + JSON |
| `tests/unit/test_mcp_tool_call_conn_compat.py` | 不走网络，固定 SDK2 API、类型、并发、MRTR、auth 分类和 owner-loop close |
| `tests/unit/test_mcp_protocol_versions.py` | 钉住 SDK 2 官方协议 registry、handshake/modern 分类和精确 pin 的接受/拒绝边界 |

fixture 禁止：

- 导入本机 sibling checkout；
- 使用真实 OA/飞书/JWT Secret；
- 固定端口；
- 默认 unit 在线下载依赖；
- 用 skip/xfail 掩盖矩阵缺口；
- 在报告中输出 bearer、完整 header、响应 body 或个人数据。

PEP 723 fixture 只在显式 `make mcp-compat` 中运行；默认 unit 使用已经同步的根环境。

---

## 4. 执行命令

```bash
# SDK2 wrapper 与协议 registry 的封闭行为测试
uv run --no-sync pytest -q \
  tests/unit/test_mcp_protocol_versions.py \
  tests/unit/test_mcp_tool_call_conn_compat.py

# inbound Server modern/legacy/security 契约
uv run --no-sync pytest -q \
  tests/unit/test_mcp_server_transport.py \
  tests/unit/test_mcp_server_security.py \
  tests/unit/test_mcp_server_datasets.py

# 两个独立锁 + 真实进程矩阵
make mcp-compat

# 仓库完成门禁
make verify
```

`make mcp-compat` 先执行：

```bash
uv lock --check --script tests/compat/mcp/legacy_server.py
uv lock --check --script tests/compat/mcp/modern_server.py
```

然后才执行矩阵。脚本的 `finally` 必须回收全部进程；完成后不应残留
`legacy_server.py`、`modern_server.py`、`multirag_server.py`。

---

## 5. 当前 22 格矩阵

| ID | Client | Server | 预期协议/行为 | 证明什么 |
|---|---|---|---|---|
| `legacy-sse-success` | MultiRAG SDK2 `mode=legacy` | FastMCP3 fixture | `2025-11-25` / SSE | 显式 legacy SSE 仍可连接并返回 structured echo |
| `modern-client-to-multirag` | 官方 SDK2 `auto` | MultiRAG FastMCP4 | `2026-07-28` | 真实 inbound 使用 discover、routing headers、无 session、structured result |
| `legacy-mode-client-to-multirag` | 官方 SDK2 `legacy` | MultiRAG FastMCP4 | `2025-11-25` | inbound dual-era 仍接受 initialize 路径，modern routing headers 不泄漏到 legacy |
| `exact-handshake-<version>-to-multirag`（4 格） | SDK2 typed wire probe | MultiRAG FastMCP4 | 四个已发布 handshake revision | 每个 revision 都完成 initialize/initialized、list、call，且 wire header 与协商结果精确一致 |
| `unknown-protocol-version-rejected` | SDK2 typed wire probe | MultiRAG FastMCP4 | `2099-01-01` | HTTP 400 / MCP `-32022`；未知 revision fail closed，不被当作 legacy |
| `legacy-http-success` | MultiRAG SDK2 `auto` | FastMCP3 fixture | `2025-11-25` | production wrapper 对 legacy-only HTTP 自动 fallback |
| `multirag-client-to-handshake-<version>`（4 格） | MultiRAG SDK2 `auto` | SDK2 typed counteroffer oracle | 四个已发布 handshake revision | production wrapper 先 discover，再接受各个真实 server counteroffer；不发送 modern routing headers |
| `modern-self-probe` | 官方 SDK2 modern | MCPServer2 | `2026-07-28` | oracle 本身确实是 modern，不把 fallback 误报为 modern |
| `multirag-client-to-modern-server` | MultiRAG SDK2 `auto` | MCPServer2 | `2026-07-28` | production wrapper 真实发送 `Mcp-Method/Mcp-Name` 且无 session |
| `modern-client-auto-fallback` | 官方 SDK2 `auto` | FastMCP3 fixture | `2025-11-25` | SDK 标准 discover -> initialize fallback |
| `auth-valid` | MultiRAG SDK2 `auto` | protected MCPServer2 | modern | bearer 被 fixture 接受且报告不泄漏 credential |
| `auth-401` | MultiRAG SDK2 `auto` | protected MCPServer2 | HTTP 401 | raw fixture 与 wrapper side-channel 均保留 authentication 类别 |
| `auth-403` | MultiRAG SDK2 `auto` | protected MCPServer2 | HTTP 403 | 与 401 区分为 authorization 类别 |
| `tool-error` | MultiRAG SDK2 `auto` | MCPServer2 | `is_error=true` | 模型可读文本与旁路 error metadata 同时保留 |
| `timeout-bounds-local-wait` | MultiRAG SDK2 `auto` | MCPServer2 | bounded local cancellation | 调用方按时返回；远端最终 `cancelled/completed` 都如实记录 |
| `modern-caller-cancel` | 官方 SDK2 modern | MCPServer2 | cooperative cancel | 显式 task cancel 的 caller/remote terminal outcome 被记录 |

关键断言不是“返回成功”，而是：

- modern：`protocol_version=2026-07-28`、`Mcp-Method=tools/call`、`Mcp-Name=<tool>`、无
  `Mcp-Session-Id`；
- handshake era：四个已发布 revision 均经过 initialize，不能出现 modern routing headers；
- `mode="legacy"` 表示选择 handshake **时代**，最终精确 revision 由 initialize 协商结果决定；官方
  Client 不允许把 handshake 日期作为 `mode` 精确 pin；
- unknown：未出现在 SDK registry 的 revision 返回结构化 400，不能用字符串大小比较或 fallback 接受；
- auth：raw HTTP status 与 wrapper `connection_status` 一致；
- timeout/cancel：本地任务和进程有界退出，远端状态不被包装层伪造。

---

## 6. Outbound Client 当前实现契约

生产入口仍是 `common/mcp_tool_call_conn.py::MCPToolCallSession`，同步调用面保持不变，但内部已经
迁到 SDK2。

### 6.1 Transport 与协商

Streamable HTTP：

```text
configured headers
  -> SDK create_mcp_http_client(headers=...)
     (connect/write/pool 30s; read 300s; follow redirects)
  -> append response hook for 401/403 only
  -> streamable_http_client(url, http_client=...)
  -> Client(mode="auto")
  -> server/discover 或 initialize fallback
```

SSE：

```text
sse_client(url, headers=...)
  -> Client(mode="legacy")
  -> initialize
```

SDK2 的 `streamable_http_client` 不再接收 `headers=`/`timeout=`；旧的
`streamablehttp_client` 名字也已经删除。调用方使用 SDK 的 `create_mcp_http_client()` 建立并拥有
`httpx2.AsyncClient`，沿用 MCP 的 30/300 秒 transport 默认值，再由外层 per-call deadline 决定
业务等待上限；不能回落到 httpx2 的 5 秒通用默认值。谁创建 client，谁负责进入和退出它。

### 6.2 生命周期

- 每个 server wrapper 仍有一个 owner event loop/thread，长期持有 SDK Client；
- 初始化 timeout 只包围 Client/transport 进入，不包围整个连接寿命；
- 工具调用直接并发进入 SDK session，不再经过一个串行 `asyncio.Queue` worker；
- `get_last_tool_call_meta()` 仍是兼容旧调用面的 session 级 latest-value，不保证并发调用逐请求
  关联；本轮只保证实际返回值、进度 accumulator 与 401/403 ContextVar 状态互不串线；
- close 使用单一 deadline：在 owner loop 取消 in-flight calls、发 shutdown、等待 AsyncExitStack
  退出，再由外部线程停止并 join owner thread；确认 thread 已退出后显式 `event_loop.close()`，
  仍在运行时绝不强关 loop；
- 构造器先用 thread event 确认 owner loop 已进入 `run_forever()`，再调度 MCP runner；因此构造后
  立即 close 也走 runner 的正常取消/退出路径，不会销毁 pending task；批量清理临时 loop 在
  helper thread join 后同样显式关闭；
- join 超时不会再无界 `shutdown(wait=True)`；Python 无法强杀被同步代码永久阻塞的线程，因此仍会
  记录错误并 fail the gate。

### 6.3 SDK2 类型

Python 属性必须使用：

- `is_error`，不是 `isError`；
- `structured_content`，不是 `structuredContent`；
- `input_schema`/`output_schema`，不是 `inputSchema`/`outputSchema`；
- `mime_type`，不是 `mimeType`。

只有输出 wire JSON 时才使用 `model_dump(by_alias=True)`。业务代码继续从 `mcp.types` 导入，避免
穿透 `mcp` 对 `mcp-types` 的依赖边界。

### 6.4 401/403

SDK2 transport 当前可能把非 2xx 归一成不含 HTTP status 的 `MCPError(-32603)`。MultiRAG 在自己
通过 `create_mcp_http_client()` 创建的 `httpx2.AsyncClient` 上安装 response hook，只在 401/403
时记录状态码；不读取或存储响应
body、credential 或完整 headers。异常对外变成 typed `MCPConnectionError.status_code`，同步调用面
分别返回 authentication/permission 文案，并在旁路 metadata 写 `connection_status`。

这只是错误分类，不是 OAuth Resource Server 实现。标准 metadata、challenge、token verification
仍属于 A3/A7。

### 6.5 Timeout 与取消

`asyncio.timeout()` 会取消本地正在等待的 SDK 调用，因此：

- 调用方有界返回；
- 旧串行 worker/HOL 已消失，其他调用可继续；
- close 能取消仍在等待的本地任务。

但 HTTP cancellation 是协作式的。server handler 可能收到取消并终止，也可能已经进入不可取消区间
而最终完成。业务写操作不能依赖 coroutine cancellation 达成 exactly-once；必须靠 M3/M4 的
prepare/execute、幂等台账和 reconciliation。

---

## 7. MRTR / `InputRequiredResult` 当前边界

Client wrapper 使用低层 `client.session.call_tool(..., allow_input_required=True)`，原因是 Host 必须
把交互暂停交给将来的 InteractionSession，而不是在没有用户 UI 的后台自动驱动多轮回调。

当前行为：

1. 收到 `InputRequiredResult`；
2. `model_dump(mode="json", by_alias=True, exclude_none=True)`；
3. 返回 `interaction_required=true` 的模型可读 JSON；
4. 旁路 metadata 保留 JSON-safe `input_requests` 和 opaque `request_state`；
5. 本次不自动重跑工具。

当前**没有**：

- durable `InteractionSession`；
- revision/CAS、TTL、owner Principal binding；
- 飞书 Form/H5 renderer；
- 收到表单后的 `input_responses + request_state` resume；
- process restart 后的 server key/state 可用性保证；
- confirmation、authorization 或 idempotency。

因此 F3 只完成协议接收面。U14 才建设 transport-neutral pause/resume，U15 才接飞书；任何
`input_required`、按钮或表单 `confirm` 都不是授权事实。

---

## 8. Inbound Server 当前实现契约

`mcp/server/server.py` 继续使用独立 FastMCP 框架的 `fastmcp.FastMCP`；不要按官方低层 SDK 的
`FastMCP -> MCPServer` rename 机械改名。

FastMCP4 同一个 HTTP server 自动支持 modern 与 legacy：

- modern `2026-07-28` 本身无 session；
- handshake era 的 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25` 仍可 initialize；
- `stateless_http=True` 只影响 legacy session 管理，不是 modern 开关；
- SSE 永远属于 legacy。

当前 security/业务边界不因升级改变：self-host 静态 API key、host 模式后端 API credential、
Host/Origin 防护、进程内限流、error redaction、read-only annotations 和 structured output 仍在；
企业 Principal、canonical resource/audience、scope、tool visibility 和分布式限流仍未完成。

---

## 9. 历史 characterization：为什么 F6/F7 必须原子决策

升级前 MultiRAG root 是 FastMCP 3.4.4 + MCP SDK 1.28.1：

- outbound 手调 `ClientSession.initialize()`；
- 所有调用进入一个串行 queue；
- timeout 只停止等待，远端调用继续占住 worker；
- 401/403 被压成普通连接字符串；
- inbound 对 SDK2 auto 只协商 legacy；
- FastMCP3 metadata 约束 `mcp<2`，不能在同一 root 单独加入 MCP2。

F6 比较三案：

1. 拆 `mcp/server` 独立 project/runtime；
2. 经批准全根同步 FastMCP4 beta + MCP2；
3. 等 FastMCP4 stable。

用户明确说明当前无生产 FastMCP3 服务，批准直接采用最新 4.x 预发布。最终选择 2，并 exact pin
b2。因为只改依赖会立即破坏旧 Client import，而只改 Client 又无法与 FastMCP3 同解，F7/F3/F8
必须在一个原子技术提交完成；逻辑任务和安全边界仍分别验收。

---

## 10. 后续任务入口

协议基础已完成。正确后续顺序不是继续笼统“升级 MCP”，而是：

```text
P1 + C3
  -> P2 MultiRAG Principal 全链传递

F3 + F4
  -> A1 两仓 JWT/JWKS claims + test vectors

A1 + F4
  -> A3 of_mcp Resource Server auth
  -> A4 of_mcp Principal/scope enforcement

A1 + P2
  -> A2 MultiRAG issuer

P2 + F3 + A2 + A4
  -> P3 request-scoped delegated credential

F8 + A1 + P1
  -> A7 MultiRAG inbound 独立 Resource Server auth

F3 + P3 + A4 + C3
  -> U14 durable InteractionSession / MRTR resume
  -> U15 Feishu Form/H5 adapter

A5 -> M1/M2 -> M3/M4
  -> prepare/confirm/execute + idempotency/reconciliation
```

FastMCP/MCP Tasks、MCP Apps、EMA/MultiAuth/Horizon 都不是 U14/U15 或敏感写操作的首期前置。

---

## 11. 停止条件

后续任何 MCP 版本、auth 或交互任务遇到以下情况立即停止并记录：

- FastMCP wrapper/slim 离开同一 exact prerelease；
- root lock 或任一 script lock 不能 `--check`；
- modern 调用无法证明 `2026-07-28` routing headers/no-session；
- 五个已发布 revision、未知版本拒绝、legacy fallback、SSE、401/403、tool error、timeout/cancel
  任一被 skip/xfail；
- 测试需要真实 Secret、外网业务服务、固定 sibling path 或固定端口；
- owner loop/thread 或 fixture 子进程不能有界退出；
- 把 `InputRequiredResult`、飞书确认或 annotations 当成鉴权；
- 为“完成升级”顺手引入 Principal/scope/EMA/Tasks/Apps，模糊独立发布面；
- 给本地 `mcp/` 添加 `__init__.py` 或改用会遮蔽官方包的 module 入口。

本文件只证明协议/runtime 基础。上线安全最终以 [TESTING_SECURITY](TESTING_SECURITY.md) 和对应
ROADMAP 任务的拒绝路径为准。
