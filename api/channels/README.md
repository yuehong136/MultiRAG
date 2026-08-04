# MultiRAG Chat Channel

`api/channels` 是 MultiRAG 的外部消息通道运行时。它把飞书等平台事件规范化后交给
MultiRAG 自己的执行边界，不在传输层拼提示词，也不让外部消息决定租户、目标、发布版本或权限。

当前提供两种运行模式：

| 模式 | 入口 | 绑定来源 | 适用场景 |
|---|---|---|---|
| Demo worker | `python -m api.channels.worker --channel feishu` | 环境变量中的固定已发布 Agent | 单机器人、本地演示、快速验证 |
| Managed supervisor | `python -m api.channels.supervisor` | MultiRAG Channel 控制面和数据库 binding | 管理页面配置、多机器人、长期部署 |

两种模式都必须作为独立进程运行，不能嵌入 Uvicorn worker。这样可避免 reload 或多
worker 重复建立飞书长连接，也能隔离 `lark-oapi` 1.x 的进程级全局事件循环。

## MultiRAG 命名与系统边界

Channel 运行时只允许以下 MultiRAG 目标类型：

- `multirag.canvas_agent`：MultiRAG 已发布的 Agent 画布及其发布版本。
- `multirag.dialog`：MultiRAG 自己的 Dialog。

兼容字段 `chat_id` 也只映射为 `multirag.dialog`。Channel 代码、数据库约束、API DTO、
日志和执行服务不得使用其他产品的运行时命名空间，也不得调用其他产品的 API、数据库或
Dialog 服务。

本包的传输布局参考了上游开源项目 commit
`d6f1475c5c1fe266a6eab2c0acee9722d6720fea`（各源文件保留其原始版权头）。该上游在这里仅是 Apache-2.0
代码来源、实现风格和后续 Git 跟进参考；MultiRAG 的控制面、binding、执行服务、状态、
安全模型和运行进程全部是本项目自己的实现。

## 共同的传输边界

```text
飞书 SDK
  -> Channel 事件规范化
  -> 有界 asyncio 队列
  -> 消息过滤、去重、会话顺序控制
  -> MultiRAG 执行边界
  -> 回复原飞书消息
```

必须遵守以下边界：

- SDK 回调只规范化事件并投递队列，不等待模型或工具执行。
- `IncomingMessage.sender_id` 是不可信的外部标识，不是已认证的 MultiRAG 用户。
- 用户消息不能覆盖租户、binding、目标、版本、session、Principal 或工具权限。
- 不把外部身份伪装成 `Principal`，也不把身份字段拼进提示词要求模型“自报身份”。
- 只接受私聊文本；群聊和机器人消息静默忽略，非文本返回固定提示。
- 发送失败、上游异常和日志均不得包含问题、答案、原始事件、凭据或完整外部 ID。
- Redis 用于去重、会话状态和单 App ID leader lease；Redis 不可用时 fail closed。
- Agent/工具可能已经开始执行后，不自动重试同一次请求；副作用工具还必须在自身边界实现幂等。

## 模式一：Demo worker

Demo 模式用于当前“小丽”替换演示。一个进程固定连接一个飞书 App，并调用一个固定的
MultiRAG 已发布 Agent：

```text
飞书长连接
  -> Redis 去重和飞书用户会话映射
  -> POST /api/v1/agents/{agent_id}/completions
  -> 聚合最终 SSE 文本
  -> 回复飞书消息
```

该模式需要标准 MultiRAG Agent API Token，不使用 beta embed token。请求固定使用
`stream=true` 和 `release=true`，且不会发送飞书身份、`user_id`、`custom_header`、
`inputs` 或 `metadata`。

### 飞书应用准备

1. 启用机器人能力。
2. 事件接收方式选择长连接。
3. 订阅 `im.message.receive_v1`。
4. 开通 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。
5. 发布应用版本，并把演示账号加入可用范围。

长连接模式不需要公网回调 URL、Verification Token 或 Encrypt Key。

### Demo 环境变量

```text
MULTIRAG_CHANNELS__FEISHU__ENABLED=true
MULTIRAG_CHANNELS__FEISHU__APP_ID=<feishu-app-id>
MULTIRAG_CHANNELS__FEISHU__APP_SECRET=<from-secret-manager>
MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL=http://127.0.0.1:8123
MULTIRAG_CHANNELS__FEISHU__AGENT_ID=<published-multirag-agent-id>
MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN=<standard-multirag-api-token>
MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER=leadership-demo-v1
MULTIRAG_CHANNELS__FEISHU__DOMAIN=feishu
MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS=[]
```

远程 API 地址必须使用 HTTPS；明文 HTTP 只接受 `localhost` 或字面量回环 IP。Base URL
必须是 origin 根地址，不得包含 userinfo、路径、查询参数或 fragment。

`ALLOWED_OPEN_IDS=[]` 表示只依赖飞书应用可用范围。定向演示建议同时使用非空白名单和
尽可能窄的飞书可用范围。

### 启动 Demo worker

Windows PowerShell：

```powershell
# 环境变量可在当前 PowerShell 或本机未提交的脚本中注入
uv run python -m api.channels.worker --channel feishu

# 仓库还提供仅供本地演示的模板
Copy-Item scripts/run_feishu_channel.example.ps1 scripts/run_feishu_channel.local.ps1
powershell -ExecutionPolicy Bypass -File scripts/run_feishu_channel.local.ps1
```

macOS、Linux 或 Ubuntu（bash/zsh）：

```bash
export MULTIRAG_CHANNELS__FEISHU__ENABLED=true
export MULTIRAG_CHANNELS__FEISHU__APP_ID='<feishu-app-id>'
export MULTIRAG_CHANNELS__FEISHU__APP_SECRET='<from-secret-manager>'
export MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL='http://127.0.0.1:8123'
export MULTIRAG_CHANNELS__FEISHU__AGENT_ID='<published-multirag-agent-id>'
export MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN='<standard-multirag-api-token>'
export MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER='leadership-demo-v1'
export MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS='[]'

uv run python -m api.channels.worker --channel feishu
```

健康启动日志应依次出现 `ws_connected` 和 `worker_started`。`/reset` 删除当前飞书会话的
Agent session 映射；重新发布 Agent 后应更新 `RELEASE_MARKER`，避免继续复用旧 DSL 会话。

Demo worker 只完成应用级认证，不能宣称已经实现飞书用户级 SQL/MCP 数据授权。

## 模式二：Managed supervisor

Managed 模式是管理页面和生产部署使用的长期架构：

```text
管理员 /settings/channels
  -> MultiRAG 公共 Channel 管理 API
  -> PostgreSQL：channel + encrypted secret + binding + runtime status

独立 Channel supervisor
  -> 私有 desired-state API（仅 binding_id/provider/generation）
  -> 每个 binding 启动一个隔离 worker 进程
  -> worker 从私有 runtime-config API 取一次解密后的飞书凭据
  -> 飞书事件
  -> POST /api/v1/internal/channel-bindings/{binding_id}/executions
  -> 服务端解析 tenant/target/revision/session
  -> PublishedTargetExecutionService
  -> multirag.canvas_agent 或 multirag.dialog
```

Supervisor 只协调 desired state，不接触飞书 App Secret。每个 child worker 只在内存中获得
自己 binding 的凭据；凭据不出现在命令行、环境变量或日志中。由于 `lark-oapi` 的限制，
一个 binding/account 对应一个独立子进程。binding 被禁用、删除或 generation 改变时，
supervisor 会停止或重启相应进程；异常退出采用有上限的指数退避。

### Canvas 发布版本兼容策略

最新上游基线中的 Canvas 执行契约仍是 `release=true` 时读取“最新已发布版本”，并不
支持按历史 revision ID 直接执行。MultiRAG Channel 不修改这条 Canvas 核心路径，也不向
`canvas_service.completion()` 增加 Channel 专用参数：

1. 管理端只能把 `multirag.canvas_agent` 绑定到当时的最新已发布版本，并把该版本 ID 保存为
   服务端 revision guard。
2. 每次执行前，Channel 适配器重新校验该 guard 仍等于最新已发布版本。
3. 校验通过后，仅调用原生 `release=true` 执行路径；不会把 revision ID 注入 Canvas 或提示词。
4. Agent 发布新版本后，旧 binding 会 fail closed。管理员更新 binding 后 generation 增加，
   服务端使用新的会话命名空间，避免复用旧 DSL 会话。

这不是“按版本精确执行历史 DSL”。在上游提供相应能力前，如确需历史版本执行，
应在 MultiRAG 自有适配层增加独立执行器并保持 Canvas 核心不变，不能直接扩写上游同步文件。

### 启动前提

1. PostgreSQL、Redis 和 MultiRAG API 已启动。
2. 已执行当前 Alembic migrations：

   ```bash
   uv run alembic upgrade head
   ```

3. MultiRAG API 进程配置了持久的 Channel 主加密密钥和内部 workload token。
4. 管理员在 `/settings/channels` 创建飞书 Channel，填写 App ID/App Secret，选择
   `multirag.canvas_agent` 或 `multirag.dialog`，然后启用 binding。
5. 独立 supervisor 能通过 HTTPS 或同机回环地址访问 MultiRAG API。

### 控制面环境变量与最小权限

生成值时只把输出写入正式 secret manager，不要粘贴到仓库文件、CLI 参数或日志：

```bash
# AES-256-GCM 主密钥：URL-safe base64 编码的 32 个随机字节
uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

# API 与 supervisor 共享的内部 workload token，至少 32 个字符
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

进程权限应按下表拆分：

| 环境变量 | MultiRAG API | Supervisor/child worker | 说明 |
|---|---:|---:|---|
| `MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY` | 必需 | 禁止 | AES-256-GCM 主密钥；必须稳定保存，当前没有自动轮换/重加密流程 |
| `MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN` | 必需 | supervisor 必需；child 自动派生 | API 与 supervisor 共享主 workload token；supervisor 为每个 child 派生仅限 binding + generation 的 token，不把主 token 传给 child |
| `MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL` | 可选 | 必需 | API origin；远程必须 HTTPS，同机开发可用 `http://127.0.0.1:8123` |
| `MULTIRAG_CHANNELS__CONTROL__RECONCILE_INTERVAL_SECONDS` | 可选 | 可选 | desired-state 对账间隔，默认 10 秒 |
| `MULTIRAG_CHANNELS__CONTROL__RUNTIME_HEARTBEAT_SECONDS` | 可选 | 可选 | worker 状态心跳间隔，默认 15 秒 |
| `MULTIRAG_CHANNELS__CONTROL__SESSION_TTL_SECONDS` | 可选 | 可选 | 服务端 binding 会话 TTL，默认 86400 秒 |
| `MULTIRAG_CHANNELS__CONTROL__DEDUPE_TTL_SECONDS` | 可选 | 可选 | 服务端 execution 幂等窗口，默认 86400 秒 |

API 进程示例（值仅表示由 secret manager 注入）：

```text
MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY=<persistent-key-from-secret-manager>
MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=<shared-workload-token>
```

Supervisor 进程示例：

```text
MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL=http://127.0.0.1:8123
MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=<shared-workload-token>
```

不要把主加密密钥发给 supervisor。即使运维平台误把它注入 supervisor，supervisor 也会在
创建 child worker 前显式删除该环境变量；正确部署仍应从源头实行最小权限。Supervisor 使用
主 workload token 拉取 desired state，启动 child 时会把它替换成 HMAC 派生的 binding +
generation 专用 token。旧 generation 或其他 binding 的 runtime-config、状态、执行和 reset
请求都会被服务端拒绝。

### Windows 启动 Managed supervisor

```powershell
$env:MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL = "http://127.0.0.1:8123"
$env:MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN = "<from-secret-manager>"

uv run python -m api.channels.supervisor

# 或使用只校验既有环境变量、不保存 secret 的跨目录启动包装器
powershell -ExecutionPolicy Bypass -File scripts/run_channel_supervisor.example.ps1
```

### macOS、Linux 或 Ubuntu 启动 Managed supervisor

```bash
export MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL='http://127.0.0.1:8123'
export MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN='<from-secret-manager>'

uv run python -m api.channels.supervisor

# 或使用只读取既有环境变量的包装器
sh scripts/run_channel_supervisor.example.sh
```

`uv run` 在所有平台执行的是同一个 Python 模块；PowerShell 脚本只是一层本地便利包装，
不是 Channel 的 Windows 专有启动机制。

### systemd 建议

Ubuntu/Linux 生产环境建议使用独立 systemd service，而不是把 supervisor 放进 API
service。仓库提供可直接安装的
`deploy/systemd/multirag-channel-supervisor.service` 和
`deploy/systemd/channel-supervisor.env.example`；示例中的路径、用户和环境文件应按部署目录调整：

```ini
[Unit]
Description=MultiRAG Channel Supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=multirag
Group=multirag
WorkingDirectory=/opt/multirag
EnvironmentFile=/etc/multirag/channel-supervisor.env
ExecStart=/opt/multirag/.venv/bin/python -m api.channels.supervisor
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
```

`/etc/multirag/channel-supervisor.env` 只应包含 runtime API base URL、主 internal token 和可选
tuning，权限设为 root/服务账号可读（例如 `0600`）；不要放主加密密钥、飞书 App Secret
或 Demo Agent Token。更成熟的环境应由 Vault/KMS/云 Secret Manager 在启动时注入。

升级后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multirag-channel-supervisor
sudo systemctl status multirag-channel-supervisor
```

### 容器和 Kubernetes 建议

- API 与 Channel supervisor 使用同一 MultiRAG 镜像、不同 command 和不同权限集合。
- Supervisor command 使用 `python -m api.channels.supervisor`；不要在容器中启动 Uvicorn
  来承载 Channel。
- 配置 init 进程和至少 30 秒 termination grace period，确保 supervisor 能终止全部 child。
- 当前一个 supervisor 会协调全部 desired bindings，因此 deployment 设为 **1 个副本**；
  滚动升级优先使用 `Recreate`。Redis leader lease 是故障保护，不应被当成多 supervisor
  主动扩容机制。
- API 容器获得主加密密钥和 internal token；supervisor 容器只获得 runtime API URL 和
  internal token。飞书凭据由 worker 经私有 API 按 binding 获取，不作为 Pod 环境变量。
- 非回环的集群内地址也必须使用 HTTPS。若必须走明文开发流量，只能把 API 放在同一
  Pod/主机并通过回环地址访问。
- 当前 supervisor 不暴露 HTTP health endpoint。进程存活用于 liveness，管理 API 中的
  `heartbeat_at`、`observed_generation` 和 `state=connected` 用于 readiness/运维判断。

## 安全模型和当前边界

### 已实现

- 管理 API 使用当前 MultiRAG 登录 Principal，并按 tenant 校验 Channel 和目标归属。
- 飞书 App Secret 只写入，数据库中使用 AES-256-GCM 加密；AAD 绑定 tenant ID 和
  channel ID，防止密文跨行搬移后仍可解密。
- 公共 Channel API 只返回 `secret.configured` 和版本，不回显密文或明文。
- 私有 desired-state API 不返回 tenant、target、revision 或凭据。
- 私有 runtime/execution API 使用 workload token；目标、租户和发布版本只从服务端
  binding 解析，不接受 worker 或用户消息覆盖。
- Supervisor 的主 workload token 不下发给 child；child token 绑定 binding ID 和 generation，
  修改配置、轮换凭据、禁用或更新 binding 后，旧 worker 会被服务端 generation fence 拒绝。
- execution 使用事件幂等键和 Redis 会话隔离；外部用户只能作为 transport actor 记录，
  不会被提升为 MultiRAG Principal。
- Supervisor 不记录原始 binding ID，worker 不记录原始飞书 ID、问题、答案或 SSE 帧。

### 尚未实现或不能宣称

- API 与 supervisor 之间的主 internal token 目前仍是静态 workload token，不等于 mTLS 或
  短期 delegated token；child token 虽已缩小作用域，仍由该主 token 确定性派生。
- 飞书用户尚未正式映射为 MultiRAG 用户 Principal。
- 尚未实现基于 `RunContext.principal` 的用户级 MCP/SQL 授权。
- 主加密密钥尚无在线轮换和存量密文重加密流程；不得直接替换旧 key。
- 当前只支持飞书私聊文本，不支持群聊、卡片流式、图片、文件或语音。

因此，生产 binding 仍应绑定只读、最小权限的 Agent/Dialog；涉及副作用的 MCP 工具必须
自行验证授权和幂等键。正式 Principal/ToolRuntime 接入后，可替换内部执行适配器，而无需
重写飞书传输、队列、状态或 supervisor。

## 运维与故障判断

- 新增或启用 binding：下一次 reconcile 启动 worker。
- 修改 App ID、App Secret、allowlist、domain、目标或发布版本：generation 增加并重启
  对应 worker。
- 禁用或删除 binding：supervisor 优雅停止对应 worker。
- Child 异常退出：supervisor 记录脱敏错误并指数退避重启。
- MultiRAG API/Redis 不可用：停止执行，不能降级到进程内无状态模式。
- internal token 轮换：协调更新 API 和 supervisor，并重启 supervisor；旧 child 派生 token
  会立即失效并由 supervisor 重建。
- 主加密密钥丢失：现有飞书凭据无法恢复；必须从 secret manager 备份恢复或重新录入。

正常日志可包含：

```text
channel_supervisor_event=worker_started
channel_event=ws_connected
channel_event=worker_started
```

日志不得包含 App Secret、internal token、tenant access token、完整 WebSocket URL、原始
事件、问题、答案、完整用户/会话/message ID 或 MCP 参数。

## 与上游同步策略

### Feishu / Lark 域名兼容

MultiRAG 的规范公开字段是 `config.domain`。为兼容新版上游的请求结构，控制面在根字段
缺失时也接受 `config.credential.domain`；两处同时存在时以根字段为准。兼容值会被提升到公开
配置，不会随 App Secret 一起进入加密凭据存储。这样国际版的 `lark` 不会因为字段位置差异，
在请求正常保存后被 Pydantic 默认值静默替换成国区 `feishu`。

| 路径 | 所有权 | 跟进方式 |
|---|---|---|
| `core/registry.py` | 上游形态、低差异 | 优先对比并语义移植注册机制变化 |
| `core/base.py` | 兼容的本地 contract fork | 保留兼容构造器和 MultiRAG 安全日志，再语义移植 |
| `feishu/channel.py` | 加固后的上游派生 | 保留 readiness、shutdown、SDK seam、raw-event 清理和日志脱敏 |
| `agent_bridge.py`、`binding_bridge.py` | MultiRAG | 不用上游文件覆盖 |
| `state_store.py`、`runtime_client.py` | MultiRAG | 不用上游文件覆盖 |
| `worker.py`、`supervisor.py` | MultiRAG | 不用上游 Bootstrap/进程模型覆盖 |
| `api/channel_control`、`api/channel_execution`、`api/channel_runtime` | MultiRAG | 作为本项目长期主线维护 |
| `api/db/services/canvas_service.py`、`user_canvas_version.py` | 上游同步核心 | Channel 不加参数、不改发布语义；只从独立适配器调用公开契约 |

跟进新版上游时：

1. 在本文件更新所参考的 upstream SHA。
2. 对比 `api/channels/core/{base,registry}.py`、`api/channels/feishu/channel.py` 的传输层变化，
   同时核对 Canvas 发布与 completion 契约是否有上游变化。
3. 按上表语义移植，不整文件覆盖加固版本。
4. 把上游产品名、模型、路由、表名和运行时值翻写为 MultiRAG 自己的实现。
5. 禁止引入任何指向上游运行服务的 HTTP、RPC、数据库或消息队列依赖。
6. 运行 Channel 契约测试和全仓验证，通过后再更新 SHA。

## 验证

Channel 相关快速验证：

```bash
uv run pytest tests/unit/test_channel_config.py tests/unit/test_channel_secret_crypto.py tests/unit/test_channel_secret_store.py
uv run pytest tests/unit/test_chat_channel_control.py tests/unit/test_channel_execution.py tests/unit/test_channel_execution_api.py
uv run pytest tests/unit/test_channel_execution_adapters.py tests/unit/test_channel_runtime_api.py tests/unit/test_channel_runtime_client.py
uv run pytest tests/unit/test_feishu_agent_bridge.py tests/unit/test_feishu_binding_bridge.py tests/unit/test_feishu_channel.py
uv run pytest tests/unit/test_feishu_state_store.py tests/unit/test_feishu_worker.py tests/unit/test_channel_supervisor.py
uv run ruff check api/channels api/channel_control api/channel_execution api/channel_runtime
```

提交前仍必须执行仓库总门禁：

```bash
make verify
```
