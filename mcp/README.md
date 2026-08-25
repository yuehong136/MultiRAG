# MultiRAG MCP 模块

本文件只描述 `mcp/` 模块**现在**的行为、运行方式和未实现边界。企业身份、跨仓授权、目标版本和
任务状态分别以 [`docs/enterprise-identity-mcp/`](../docs/enterprise-identity-mcp/README.md)、
[`VERSION_BASELINE`](../docs/enterprise-identity-mcp/VERSION_BASELINE.md) 和
[`ROADMAP`](../docs/enterprise-identity-mcp/ROADMAP.md) 为准。

## 两个方向不要混用

MultiRAG 同时出现在 MCP 的两个方向：

| 方向 | 生产入口 | 当前责任 |
|---|---|---|
| 出站 Host/Client | [`common/mcp_tool_call_conn.py`](../common/mcp_tool_call_conn.py) | Agent 调用外部 MCP Server；`mcp/client/` 下两个文件只是本模块入站 Server 的示例/兼容客户端 |
| 入站 Resource Server | [`mcp/server/server.py`](server/server.py) | 向外部 MCP Client 暴露 MultiRAG 数据集目录和检索工具 |

两个方向不得复用 bearer、MCP connection/session、audience、scope policy 或用户状态。架构定案见
[EIM-ADR-17](../docs/enterprise-identity-mcp/DECISIONS.md#eim-adr-17multirag-的-mcp-hostclient-与-mcp-resource-server-是独立安全和发布面)。

本目录故意不创建顶层 `mcp/__init__.py`。生产代码里的 `import mcp` 必须解析到官方 MCP Python SDK；
把本目录变成同名 Python package 会遮蔽第三方依赖，破坏 Client/Server 导入与兼容测试。示例客户端应以
脚本运行，不得通过新增 `__init__.py` 把本目录包化。

## 当前入站 Server

入口：

```bash
uv run mcp/server/server.py \
  --host=127.0.0.1 --port=9382 \
  --base-url=http://127.0.0.1:8123 \
  --mode=self-host --api-key=multirag-example
```

端点与传输：

- `/mcp`：Streamable HTTP，默认 `stateless_http=True`、JSON response；已实测通过 MCP
  `2026-07-28` 的 `server/discover`、`Mcp-Protocol-Version` / `Mcp-Method` / `Mcp-Name`
  路由头与无 `Mcp-Session-Id` 请求；
- `/sse`：只为存量消费者保留的 legacy SSE；
- `/health`：进程健康和 launch mode，不证明后端检索或授权可用；
- 默认同时开放 `/mcp` 与 `/sse`，可通过 CLI/env 独立关闭；未完成调用方审计前不要删除 legacy
  endpoint。

工具与 resource：

- `list_datasets`：列出当前 credential 可见的数据集；
- `multirag_retrieval`：对指定数据集执行检索；
- `datasets://list`：与 `list_datasets` 同源的数据集 resource；
- 工具使用 Pydantic 返回模型，提供字段级 `outputSchema` 和 `structuredContent`；两个工具当前都标为
  read-only/idempotent hints。annotations 是客户端提示，不代替服务端授权。

安全与运行时：

- 全仓精确使用 `fastmcp==4.0.0b3` 和 `mcp==2.0.0`；FastMCP 4 仍为 beta，因此锁定版本和
  modern/legacy 双向回归都是运行边界的一部分；
- FastMCP error middleware 隐藏底层异常细节；结构化日志不包含 payload；
- Streamable HTTP 保留 Host/Origin DNS-rebinding 防护；公网域名必须显式配置 allowlist；
- 当前限流是进程内桶，多 worker 之间不共享；
- self-host 模式使用一个静态 API key，并由 FastMCP verifier 校验；
- host 模式从每个请求读取 Bearer/API key，并调用 MultiRAG REST API 做实际资源访问；这里的 credential
  仍是现有 MultiRAG API credential，不是已经落地的企业 Principal/scope 委托；
- legacy `api_key`/`x-api-key` 只由 compatibility middleware 规范化到 Bearer 路径，不应成为新集成。

## 当前出站 Client

生产 Agent 不使用 `mcp/client/` 示例，而是通过 `common/mcp_tool_call_conn.py`：

- Streamable HTTP 创建官方 SDK 2 `Client(mode="auto")`，由 SDK 先尝试 modern
  `server/discover`并对 legacy server 回退；SSE 显式使用 `mode="legacy"`；业务代码不再手调
  `initialize()`；
- Streamable HTTP 的 headers 由 SDK `create_mcp_http_client()` 创建的受管 client 携带，保留
  MCP 30 秒 connect/write/pool、300 秒 read 默认值；response hook 保留 HTTP `401`/`403`
  分类，调用方可从错误文本和旁路 metadata 区分未认证与已认证但被拒绝；
- MCP URL、variables、server headers 和 custom headers 来自已保存的 server 配置；
- `structuredContent` 会被保留到旁路 metadata，但主要工具返回仍聚合为模型可读字符串；
- 旧的每 Server 串行队列已删除；同一 Client 上的调用可并发发送，不再因一个慢请求形成
  队列头阻塞（HOL）；
- `get_last_tool_call_meta()` 仍只是 session 级“最后完成值”，不保证并发调用逐请求关联；并发
  上层应以各自调用返回值为准，后续事件化 Host 不应依赖这个兼容旁路做 durable correlation；
- 超时会取消本地调用 task 并从 in-flight 集合清理；远端是否停止仍取决于 transport/server
  的协作式取消，超时不得被解读为远端未执行；
- `InputRequiredResult` 现在会以 `interaction_required` 结构和旁路 metadata 表面化，供后续 Host
  编排识别；当前不持久化、不自动补参重试，也不提供 resume；
- 当前仍没有 request-scoped Principal credential provider 或 `InteractionSession`。

## 尚未实现或不能宣称

- 入站 `/mcp` 已实现并验证 MCP `2026-07-28` modern 基线，但这只是协议/传输事实，
  **不证明**企业身份、OAuth Resource Server、scope 或 MRTR Host 已完成；legacy HTTP/SSE
  兼容仍保留并回归；
- 入站 Server 尚未提供企业级 protected-resource metadata、独立 canonical audience、Principal、
  tool scope/step-up 和跨 worker 分布式限流；
- 出站 Client 已迁移到官方 MCP SDK 2，但尚无 Principal/token/scope 链，因此仍不能代表
  当前飞书用户已被安全委托到 `of_mcp`；
- 两个方向仍共享主仓依赖解析面；EIM-F6/F7 已选择并实施整仓协调升级，未拆独立
  Server runtime。回滚因此是整个技术切换面，不得把 outbound/inbound 任一方单独降级到 SDK 1；
- 本模块没有 MCP InteractionSession、飞书 form/H5 renderer、Confirmation Store 或端到端副作用
  幂等。这些属于 EIM 项目，不应直接塞进 `mcp/server/server.py`；
- FastMCP/SDK 提供的 auth、provider 或 middleware 不能替代 MultiRAG tenant/Principal，也不能替代
  业务系统/PDP 的对象级授权。

## 验证入口

出站 Client 与入站 Server 的封闭回归：

```bash
uv run pytest -q \
  tests/unit/test_mcp_tool_call_conn_compat.py \
  tests/unit/test_mcp_server_transport.py \
  tests/unit/test_mcp_server_security.py \
  tests/unit/test_mcp_server_datasets.py
```

真实 loopback 子进程的 modern/legacy 双方向矩阵（含协商、路由 headers、无 session、
401/403、structured result 和 timeout/cancellation）：

```bash
make mcp-compat
```

`InputRequiredResult` 的 SDK2 类型、JSON-safe 旁路 metadata 和“不自动重跑”边界由上面的封闭
`test_mcp_tool_call_conn_compat.py` 固定；当前跨进程矩阵不宣称已经实现 MRTR Host 或恢复状态机。

任何实现任务完成前仍必须按仓库根 [`AGENTS.md`](../AGENTS.md) 执行 `make verify`；改到 DB、存储或
检索路径时另跑 `make integration`。版本、身份、授权或 MRTR 任务还必须执行对应 ROADMAP 行声明的
现代/legacy、正反授权和跨仓契约测试。
