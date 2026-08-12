# 技术版本与上游基线

> **基础版本核验：2026-08-07；飞书 SDK、官方文档和交互参考仓刷新：2026-08-09；
> 对话执行、重新生成和持久化参考刷新：2026-08-10；MCP/FastMCP/扩展边界刷新：2026-08-12；
> `lark-oapi` 1.7.2 可执行契约刷新：2026-08-12
> （Asia/Shanghai）**
> 版本会变化。本文记录的是可复现快照和选型规则，不是“永远最新”的承诺。

---

## 1. 当前与目标版本

| 组件 | 当前仓库 | 最近核验的官方最新 | 本项目目标 | 处理方式 |
|---|---|---|---|---|
| Python | MultiRAG `>=3.12,<3.14`；of_mcp `>=3.12` | — | 保持各仓声明范围 | 不降级 |
| `lark-oapi` | 声明 `>=1.7.2,<2`，lock 为 **1.7.2** | **1.7.2** | 已达成 | EIM-F1 已完成；I4 再实现 Provider/cache/single-flight |
| `lark-channel-sdk` | 未安装 | **1.2.0** | `>=1.2.0,<2` | EIM-C5 PoC 通过后才引入 |
| MCP Python SDK `mcp` | MultiRAG 与 of_mcp 均 exact `2.0.0` | **2.0.0 stable** | 已达成；MultiRAG outbound 使用官方 `Client` | F3 已完成；后续升级单独重跑双时代矩阵 |
| `mcp-types` | 两仓 lock 均为 2.0.0（由 `mcp` 精确约束） | **2.0.0 stable** | 与实际 SDK/框架锁一致 | 业务代码从 `mcp.types` 导入；不重复直依赖 |
| FastMCP stable | 仅隔离 legacy fixture exact 3.4.7 | **3.4.7** | 只作 legacy compatibility oracle | PEP 723 lock，不进入 MultiRAG 生产根环境 |
| FastMCP 4 prerelease | MultiRAG 与 of_mcp 均 exact 4.0.0b2；MultiRAG 另 constraint `fastmcp-slim==4.0.0b2` | **4.0.0b2 beta** | 当前已验证基线 | F4/F7 已完成；b3/RC/GA 必须另立显式版本任务 |
| MCP 协议 | MultiRAG inbound/outbound modern 主路径为 `2026-07-28`；HTTP/SSE legacy 门禁为 `2025-11-25` | **2026-07-28** | modern 主路径 + 明确 legacy compatibility | 13 格真实进程矩阵必须同时证明 modern 和 fallback，不以调用成功替代协商证据 |

版本来源：

- [`lark-channel-sdk` PyPI](https://pypi.org/project/lark-channel-sdk/)
- [`lark-oapi` PyPI](https://pypi.org/project/lark-oapi/)
- [`mcp` PyPI](https://pypi.org/project/mcp/)
- [`fastmcp` PyPI](https://pypi.org/project/fastmcp/)
- [MCP Python SDK v2 What's New](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)
- [MCP 2026-07-28 发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

### 为什么 MultiRAG 新客户端直接依赖 `mcp`，不以 FastMCP Client 为核心

MultiRAG `common/mcp_tool_call_conn.py` 已直接使用 MCP SDK 2 的一等 `Client`：Streamable HTTP
使用 `mode="auto"`，自动选择现代 `server/discover` 或回退旧 `initialize`；SSE 明确使用
`mode="legacy"`。客户端不再手调 `ClientSession.initialize()`。直接使用标准 SDK，仍可把
MultiRAG Host 逻辑与 of_mcp 的 FastMCP server 实现细节解耦。

FastMCP 继续作为 of_mcp 的 server/composition 框架，但授权契约必须基于标准 HTTP/OAuth/MCP，
不得使用只有某个 FastMCP beta 才认识的私有 Header 作为唯一方案。

迁移时已经实证：旧 FastMCP 3.4.4 约束 `mcp<2`，不能只升级 outbound Client。EIM-F6 比较了
独立 server runtime、全根协同 beta 与等待 stable 三案；用户明确确认当前无生产 FastMCP 3
服务负担，并批准全根原子升级。EIM-F7/F3/F8 因而在同一技术提交中 exact pin FastMCP 4.0.0b2
与 MCP SDK 2.0.0，同时迁完直接 API。FastMCP 3.4.7 只保留在隔离、确定锁定的 compatibility
fixture 中。该决策不意味着以后可静默跟随新 beta；每个 b3/RC/GA 都要重新做显式版本任务。

本仓顶层目录也叫 `mcp/`，但保持**无 `__init__.py`**，因此不会遮蔽 site-packages 中官方
regular package。入站服务继续以 `python mcp/server/server.py`/`uv run mcp/server/server.py`
文件入口启动；不得改成 `python -m mcp.server.server`，也不得给本地目录补 `__init__.py`。

### “MCP 2.0”应该怎么说

`mcp==2.0.0` 是 **Python SDK 的 major 版本**；`2026-07-28` 是 **MCP 协议修订版**。官方没有一个
名为“MCP 2.0 企业服务中心版”的总开关，也不存在先把整个项目一次性“全面升级成 MCP 2.0”就自动
获得身份、网关、多租户、表单和持久任务的路径。本项目应准确表述为：采用 MCP 2026 的无会话/
MRTR 等协议能力，把 MultiRAG client 迁到 Python SDK v2，并分别建设 Principal、OAuth resource
server、授权策略、持久交互和企业身份扩展。

---

## 2. 官方上游提交快照

上游版本分三层记录，禁止混用：

1. **历史来源基线**：项目最初派生或某阶段建立时使用的 SHA，保留为来源事实，不随升级覆盖；
2. **滚动兼容审计基线**：下表最近一次完成语义核验的固定快照，可在完成新审计后更新；
3. **单次移植 commit**：每个具体跟进任务实际采用、语义移植或拒绝的上游 commit，记录在任务日志。

以下 SHA 由 `git ls-remote <repo> HEAD` 获取，是第二类滚动兼容审计基线；飞书交互相关仓于
2026-08-09 刷新。后续参考源码时，先用 SHA 重现本文看到的行为，再对比最新 HEAD，避免文档链接
随 main 漂移。不得只因上游 HEAD 前进就更新本表；必须先完成对应契约与差异核验。

特别注意：下表的 RAGFlow SHA 是**外部滚动兼容审计快照**，不是 MultiRAG 当前逐 commit 同步到的
代码位置。当前本地上游同步进度约停在 2026-04-24；恢复同步时先确定准确起始 commit，再按顺序逐个
跟进。EIM-F5 / CHN-X14 当前保持挂起，只有 Channel 完成 U15 rollout/真实 smoke、CHN-U16、
CHN-O9 和稳定浸泡，且用户明确恢复这条同步主线后，才重新获取 HEAD 并更新审计结论。
（进度：CHN-U16 已于 2026-08-11 完成；真实 smoke、CHN-O9 与稳定浸泡仍未完成，挂起条件未解除。）

| 项目 | 快照 SHA | 用途 |
|---|---|---|
| `larksuite/channel-sdk-python` | `731d459cca55ac76e85911bba2b1666508145e03` | 飞书 Channel SDK、strict security、卡片与去重 |
| `larksuite/oapi-sdk-python` | `8d6402635d0a9314ddae765ae64931aabca30f79` | 通讯录 V3、token 生命周期、完整 OpenAPI |
| `larksuite/openclaw-lark` | `dde0be3680d6fd5443cab426c8f4b3216266346a` | 流式卡片、敏感确认、飞书资源工具和安全警告 |
| `openclaw/openclaw` | `73bdb4b924f6db3c4ab45c5e40fbf61b06fa56a0` | 生产 Feishu channel 能力矩阵、typing/streaming/media/thread policy |
| `bytedance/deer-flow` | `17531d7c118d6111b863f945ff910a7889a235b0` | `channel_connections`、run/thread 分离、重新生成 checkpoint、单活与取消 CAS |
| `langbot-app/LangBot` | `e37987215e8465818e373fa523075b5482b70a6e` | 多 Provider、访问控制、Lark WS/Markdown 与运维面 |
| `shareAI-lab/lark-channel` | `cf056995730a3775529c3bf87fce8033cea554a4` | 群组/线程隔离、工具过程流式卡片 |
| `modelcontextprotocol/python-sdk` | `a4f4ccd091138771535e17191123f20b30fda68e` | MCP SDK v2 客户端、双协议兼容和 OAuth |
| `infiniflow/ragflow` | `b5bffa0fa3213bbc0fee046422c7de4a3db2e39c` | 滚动兼容审计基线：Dialog no-store/全量历史与 Canvas 自持久化差异；F5/X14 当前挂起，待稳定闸门和用户恢复逐 commit 同步后才刷新 |
| `open-webui/open-webui` | `01f4282f1ffe0d6212f58d3afbeae21fffd0c4be` | 独立消息表、parent/children 分支和重新生成语义对照 |

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
git ls-remote https://github.com/infiniflow/ragflow.git HEAD
git ls-remote https://github.com/bytedance/deer-flow.git HEAD
git ls-remote https://github.com/open-webui/open-webui.git HEAD
git ls-remote https://github.com/langbot-app/LangBot.git HEAD
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

- Contact V3 `GET /open-apis/contact/v3/users/:user_id`；
- tenant access token 生命周期；
- IM reply/create 的 `uuid`、`reply_in_thread`，消息 reaction、CardKit create/update/finish；
- 消息图片/文件/音视频资源上传下载；
- 用户状态和 employee_no；
- 通讯录 created/updated/deleted/scope events；
- 未来可选的飞书文档、日历等 OpenAPI。

EIM-F1 已把根依赖与 lock 对齐到 1.7.2，并用本地固定 fixture 验证
`GetUserRequest(user_id_type="open_id")` 指向 `GET /open-apis/contact/v3/users/:user_id`，成功响应按
`GetUserResponse -> GetUserResponseBody -> User -> UserStatus` typed model 解码；fixture 只保留
`open_id/user_id/employee_no/status` 白名单字段，不包含真实租户、用户或 Secret。该契约只为 I4 固定
官方 SDK seam，不代表 Contact Provider、目录缓存或身份链已实现。

1.7.2 的顶层 `lark_oapi` import 会加载 WebSocket 模块并安装一个模块级 event loop。隔离进程实证该
loop 保持 idle、未运行/未关闭、无 task 且不启动新 thread，Client build 也不改变这一点。平台模块
`api.channel_control`、`api.channel_providers`、`api.channels.verification`、`api.identity` 必须继续
不加载任何 `lark_oapi*` 模块，并在调用前没有 event loop 的进程中保持 loop 仍不存在。这里固定的是
“已知、隔离的 idle-loop 副作用”，不能宣称 SDK 顶层 import 完全无副作用，也不能把 SDK eager import
扩散到 API/control/identity 进程。

SDK `TokenManager` 已提供进程内 token cache 和提前过期，但 1.7.2 的 cache-miss 路径是直接取 token、
再写 cache，没有锁或 single-flight。F1 不重写官方 token client；I4 必须在项目 Provider adapter 层按
Provider Account scope 实现并发折叠、故障传播和隔离测试。已有 CardKit/IM typed API 仍由 EIM-U1
基线覆盖，F1 不迁移 `lark-channel-sdk` transport。

F1 完成证据：新 contract **7 passed**，广义 Feishu/Channel 定向 **101 passed**；
`uv lock --check` 通过；`make verify` 的 Ruff format/check（1222 files）、7 import contracts、async DB
gate、mypy 73 source files 全绿，unit **2012 passed in 30.95s**。

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
- 多轮工具结果（MRTR）允许工具返回 `input_required`，由 host 在后续请求携带
  `inputResponses` 恢复；它是补充输入的协议机制，不是授权、幂等或任务存储。

官方入口：

- [2026-07-28 规范发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Authorization Extensions](https://modelcontextprotocol.io/extensions/auth/overview)
- [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)

在官方站点尚未把所有导航页切到 `2026-07-28` 路径时，以 2026 发布说明、SDK v2 文档和最终
SEP 为准，不能因此退回旧 session 设计。

### 标准、扩展、实现和产品边界（2026-08-12）

| 名称 | 当前边界 | 本项目采用时机 |
|---|---|---|
| MRTR | MCP 2026 核心协议能力；跨请求补充工具输入 | U14 用它映射 transport-neutral pause/resume；授权、持久化和幂等仍由两仓实现 |
| EMA | MCP 官方稳定 auth extension；把真实企业 IdP assertion 引入授权流程 | 仅当真实 enterprise IdP/多 issuer 需求成立时进入 A8；飞书事件字段不能伪装 assertion |
| ID-JAG | EMA 依赖的 IETF Identity Assertion Authorization Grant 工作项，当前仍须按 draft 状态治理 | A8 每次实施前重核 IETF 版本、支持库和安全模型，不锁死未定稿字段 |
| MultiAuth | FastMCP 的多 auth provider 组合实现，不是 MCP 标准 | 只有多 issuer/多入口路由确有需求且 fail-closed 测试成立时评估 |
| Horizon | Prefect/FastMCP 托管平台，提供托管、认证、访问控制和 registry；不是 MCP 标准 | 仅作运维 build-vs-buy；不作为 MultiRAG/of_mcp 运行时依赖 |
| MCP Apps | 官方 extension，当前稳定 spec 为 2026-01-26；定义 host 承载受控 web UI，必须由 host 显式支持 | 飞书 CardKit 不是 Apps host；未来独立 Web host 需要富 UI 时另立项 |
| Tasks | 2026 发布说明/SEP 已指向官方扩展，但当前 `ext-tasks` 仍标 experimental/not official，Python SDK v2 也未实现 | 按最低共同成熟度视为实验性；首期 U14/U15、确认和 run ledger 不依赖它 |

---

## 6. 升级闸门

### `lark-oapi`

- 现有飞书 Channel 单元测试全绿；
- 根声明与 lock 精确解析到 1.7.2，且现有 IM/CardKit Channel 契约不回归；
- 固定、无真实 PII/Secret 的 Contact V3 fixture 通过官方 typed model 解码，并钉住
  `open_id -> user_id/status/employee_no` request/response seam；
- 平台 control/provider/verification/identity import 不加载 SDK，也不安装 event loop；
- SDK 顶层 import 的已知模块级 loop 必须保持 idle、无 task、无新增 thread，Client build 不启动它；
- 1.7.2 `TokenManager` 只有 cache、没有 cache-miss single-flight；并发刷新测试和项目级折叠属于 I4，
  不能为了把 F1 写成完成而假称 SDK 已提供该能力。

### `lark-channel-sdk`

- SDK 回调在 3 秒内只规范化并入队；
- 消息 ID 去重责任没有与现有 Redis store 冲突；
- 多 worker/leader lease 行为实测；
- `audit` 无未解释告警后才进 `strict`；
- 回滚到现有 transport 不改变上层 `IncomingMessage` 契约。
- 对 `channel.stream()` 的节流、sequence、finish、取消和错误行为做 contract comparison；即使比
  现有 renderer 更方便，也不能把 EIM-U1 重新变成 C5 的依赖。

### MCP SDK 2 与依赖拓扑

- 隔离 FastMCP 3.4.7 与官方 MCP 2.0.0 script locks 必须继续 `uv lock --check --script`；
- 同时验证 MultiRAG SDK2 Client -> modern/legacy server、官方 SDK2 Client -> MultiRAG
  FastMCP4 modern/legacy inbound；SSE 只保留 legacy 冒烟；
- 每格记录 `server/discover`、legacy `initialize` 等实际协商路径，不能只断言工具返回值；
- list/call/tool error、取消、超时和认证 401/403 行为固定；
- 请求级 token 不被跨 Principal 复用；
- HTTP 自定义 header 只能放进调用方通过 SDK `create_mcp_http_client()` 创建并拥有的
  `httpx2.AsyncClient`；必须保留 MCP 30/300 秒 transport 默认值，不能回落到通用 5 秒默认值；
  401/403 response hook 只记录状态码，不记录 credential、body 或完整 header；
- `InputRequiredResult` 目前只序列化到旁路 metadata 交给 Host；没有 U14 持久 InteractionSession
  前，不得自动重跑或宣称可以跨进程恢复；
- timeout 必须取消本地底层调用并消除旧串行队列/HOL；HTTP 远端 handler 是否终止是协作式语义，
  matrix 要记录最终 `cancelled/completed`，不能伪造“远端一定取消”；
- inbound modern `server/discover`、`Mcp-Method/Mcp-Name`、无 session、structured result 与 legacy
  initialize 都要保留；A7 Principal/scope 尚未实现。

以下任一情况立即停止依赖解析并请用户决策，不得靠放宽范围或隐式升级“解出来”：

- resolver 需要把任一生产依赖切到未批准的 prerelease/major，或让 FastMCP wrapper/slim 离开同一
  exact beta；
- 只有 editable sibling path、已有本机 cache 或未提交 lock 才能安装，干净环境不能复现；
- 根 lock、两个 script lock 任一不可确定复现，或当前文件入口启动/回滚失败；
- compatibility gate 只得到“调用成功”，无法证明 modern 或 legacy 分支，或取消/close 留下悬挂线程；
- Tasks/Apps/EMA 等扩展的官方状态、SDK 支持和本项目假设不一致，却需要把它们当生产硬依赖。

### FastMCP 4 beta

- 完整执行 `of_mcp` 仓自己的 `AGENTS.md` 中的实测修正回归；
- mount/proxy 契约逐字节等价；
- `provider_error_strategy`、FileSystemProvider、依赖注入和 auth middleware 行为复测；
- `uv run ofmcp verify` 全绿；
- 版本升级 PR 不包含任何身份/授权功能变更。
