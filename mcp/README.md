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

## 当前入站 Server

入口：

```bash
uv run mcp/server/server.py \
  --host=127.0.0.1 --port=9382 \
  --base-url=http://127.0.0.1:8123 \
  --mode=self-host --api-key=multirag-example
```

端点与传输：

- `/mcp`：Streamable HTTP，当前默认 `stateless_http=True`、JSON response；
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

- FastMCP error middleware 隐藏底层异常细节；结构化日志不包含 payload；
- Streamable HTTP 保留 Host/Origin DNS-rebinding 防护；公网域名必须显式配置 allowlist；
- 当前限流是进程内桶，多 worker 之间不共享；
- self-host 模式使用一个静态 API key，并由 FastMCP verifier 校验；
- host 模式从每个请求读取 Bearer/API key，并调用 MultiRAG REST API 做实际资源访问；这里的 credential
  仍是现有 MultiRAG API credential，不是已经落地的企业 Principal/scope 委托；
- legacy `api_key`/`x-api-key` 只由 compatibility middleware 规范化到 Bearer 路径，不应成为新集成。

## 当前出站 Client

生产 Agent 不使用 `mcp/client/` 示例，而是通过 `common/mcp_tool_call_conn.py`：

- 当前创建长生命周期 `ClientSession` 并显式 `initialize()`；
- MCP URL、variables、server headers 和 custom headers 来自已保存的 server 配置；
- `structuredContent` 会被保留到旁路 metadata，但主要工具返回仍聚合为模型可读字符串；
- 当前没有 request-scoped Principal credential provider、MRTR `InputRequiredResult`/resume 或
  InteractionSession。

## 尚未实现或不能宣称

- `stateless_http=True` 只描述当前 HTTP transport 行为，**不证明**入站 Server 已全面实现 MCP
  2026-07-28；现有兼容测试仍覆盖 legacy initialize/session 语义；
- 入站 Server 尚未提供企业级 protected-resource metadata、独立 canonical audience、Principal、
  tool scope/step-up 和跨 worker 分布式限流；
- 出站 Client 尚未迁移到官方 MCP SDK v2，也不能代表当前飞书用户安全调用 `of_mcp`；
- 两个方向当前共享主仓依赖解析面；在升级 MCP SDK/FastMCP 前，必须先按 ROADMAP 的兼容任务证明
  依赖可解、部署可分和回滚互不绑定；
- 本模块没有 MCP InteractionSession、飞书 form/H5 renderer、Confirmation Store 或端到端副作用
  幂等。这些属于 EIM 项目，不应直接塞进 `mcp/server/server.py`；
- FastMCP/SDK 提供的 auth、provider 或 middleware 不能替代 MultiRAG tenant/Principal，也不能替代
  业务系统/PDP 的对象级授权。

## 验证入口

与入站 Server 直接相关的现有回归：

```bash
uv run pytest -q \
  tests/unit/test_mcp_server_transport.py \
  tests/unit/test_mcp_server_security.py \
  tests/unit/test_mcp_server_datasets.py
```

任何实现任务完成前仍必须按仓库根 [`AGENTS.md`](../AGENTS.md) 执行 `make verify`；改到 DB、存储或
检索路径时另跑 `make integration`。版本、身份、授权或 MRTR 任务还必须执行对应 ROADMAP 行声明的
现代/legacy、正反授权和跨仓契约测试。
