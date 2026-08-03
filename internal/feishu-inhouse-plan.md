# 飞书接入：自建落地方案（最终建议）

> 生成日期：2026-07-28
> 前置：[多租户评估](multitenancy-and-feishu-report.md) · [生态调研](feishu-ecosystem-analysis.md)
> 本文是本地评审笔记（`internal/*.md`，不入库）

**约束**：不额外部署 AstrBot / LangBot 去调用 MultiRAG，而是学它们的做法，把飞书能力
集成进 MultiRAG 自身。

---

## 1. 结论

**自建，但一行协议代码都不要自己写。** 分三层，来源各不相同：

| 层 | 内容 | 怎么解决 | 工作量 |
|---|---|---|---|
| **协议层** | tenant_access_token 生命周期、事件验签/解密、WebSocket 长连接、全部 OpenAPI 调用 | **用飞书官方 `lark-oapi`（MIT）**，不自己写 | ~0 |
| **产品层** | 消息去重、卡片流式节流、卡片状态机、会话映射、消息类型转换、群聊/私聊语义 | **抄 LangBot（Apache-2.0，可直接复制 + 署名）** | 中 |
| **权限层** | 飞书身份 → 内部用户、可见知识库计算、审计 | **只能自己写**，生态里没人做过 | 大 |

三层里只有第三层是真正的自研，而它本来就得自研——**这一点无论选哪条路都躲不掉**。

---

## 2. 一个必须先说清楚的变化：AGPL 的风险等级变了

上一轮你说"内部自用不商业化，不考虑法务"，我同意了。**但那个结论是针对"部署 AstrBot"的。
"把 AstrBot 的代码复制进 MultiRAG 仓库"是另一回事，风险高得多。**

- **部署** AGPL 软件供内部员工使用：风险低（是否构成"向公众分发"有争议，但内部场景通常站得住）
- **复制** AGPL 代码进你们 Apache-2.0 的仓库：**污染的是代码库本身**。一旦以后要交付客户、
  开源、或走投融资尽调，这段血统就是个说不清的问题，而且到时候已经很难剥离

所以在"抄代码进自己仓库"这个新前提下：

> **AstrBot 当参考资料读，LangBot 当代码来源。**
>
> 读代码学做法、然后自己重写，法律上完全没问题（版权保护表达，不保护功能和思路）。
> 逐行复制才有问题。LangBot 是 Apache-2.0，可以直接复制，保留原始版权头 + 注明来源即可。

这条把上一轮"推荐 AstrBot"的结论翻回来了——**上一轮 AstrBot 胜出的唯一理由是
`@register_provider_adapter` 能干净地做逐请求身份透传。而自建之后，这个问题根本不存在了**
（见 §5）。AstrBot 最大的优势被前提变化抹掉了。

---

## 3. 依赖选型

```toml
lark-oapi = ">=1.7.1"    # 或 lark-oapi[fastapi]
```

| 项 | 值 |
|---|---|
| 仓库 | [larksuite/oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python)（官方，默认分支 `v2_main`） |
| License | **MIT** |
| 最新版 | 1.7.1 |
| Python | >=3.8 |
| 依赖 | `requests`、`requests_toolbelt`、`pycryptodome`、`websockets<16,>=11`、`httpx<1.0,>=0.24` |
| extras | `[fastapi]`（含 fastapi + uvicorn）、`[aiohttp]`、`[flask]` |

`httpx` 我们已经有了（`pyproject.toml:61`），新增的实际只有 `pycryptodome` 和 `websockets`，
都很轻。

**SDK 帮我们挡掉的**：
- token 自动刷新（`tenant_access_token` 2 小时有效期不用自己管）
- 事件验签 + 解密
- **长连接模式下验签解密只在建连时做一次，后续事件是明文推送**——开发期完全不用碰加解密
- `ws.Client(APP_ID, APP_SECRET, event_handler=...)` + `cli.start()` 就能收事件
- 全部 OpenAPI 的类型化调用

---

## 4. 架构落点

### 4.1 代码位置

```
api/apps/feishu_app.py                 # Webhook 端点 + card.action.trigger 卡片回调
api/apps/services/feishu_service.py    # 事件分发、幂等去重、身份映射、会话映射、编排
common/im/feishu/
  ├── client.py                        # lark-oapi 封装（多应用实例管理）
  ├── card.py                          # CardKit 流式卡片状态机 + 节流器
  ├── message.py                       # 消息类型转换（text / post 富文本 / image）
  └── identity.py                      # open_id/union_id ↔ 内部 user 解析 + Redis 缓存
core/svr/feishu_svr.py                 # 长连接模式入口（单副本进程）
configs/alembic/versions/xxxx_add_feishu_tables.py
```

`core/svr/` 已经是"IM bot 独立入口"的既有位置（那里有个 `discord_svr.py`），
所以 `feishu_svr.py` 放这里是**沿用既有约定，不是新造结构**。

### 4.2 两种运行模式 —— 这解决了"要不要独立服务"的纠结

我在第一版报告里建议做成独立服务，理由是"长连接有状态、跟 FastAPI 多副本冲突"。
**用双模式就把这个理由消掉了**：

| 模式 | 载体 | 副本 | 用途 |
|---|---|---|---|
| **Webhook** | `api/apps/feishu_app.py`，跟 API server 一起跑 | 随 API server 多副本 | **生产** |
| **长连接** | `core/svr/feishu_svr.py`，独立进程 | **必须单副本** | 开发 / PoC / 小规模 |

⚠️ 长连接是集群模式**且不广播**——多副本时只有随机一个收到消息。这是飞书官方行为，
不是 bug，**生产必须用 Webhook**。

两条路汇到同一个 `feishu_service`，业务逻辑只写一遍。

而且 MultiRAG 本来就是多进程部署（API server + task_executor），
**加一个入口进程不算"多部署一个项目"**——同一个仓库、同一个镜像、同一份配置、同一套监控。

### 4.3 异步处理

飞书对事件回调有响应时间约束，RAG 一轮 5~30 秒，必须**先回 200 再异步处理**。

建议复用现成的 **Redis Stream**（`core/svr/task_executor.py` 那一套），而不是
FastAPI `BackgroundTasks`——后者在多副本 + 长任务下不好观测、进程重启就丢。
走 Redis Stream 能跟现有 task 体系共用重试、监控、死信。

### 4.4 数据表

```
FeishuApp        id, tenant_id, app_id, app_secret_enc, encrypt_key, verification_token,
                 default_dialog_id, default_agent_id, mode(webhook|ws), enabled
                 ↑ 支持多应用 → 每部门一个飞书应用也能撑（形态 1 的兜底）

FeishuIdentity   union_id(PK), open_id, feishu_user_id, multirag_user_id,
                 department_ids(JSONB), email, status, synced_at
                 ↑ 主键用 union_id：open_id 是应用维度的，换应用就变

FeishuSession    chat_id, thread_id, conversation_id, dialog_id, last_active_at
                 ↑ 飞书会话 ↔ MultiRAG 会话映射
```

用 Alembic 迁移，`configs/alembic/versions/` 已有 33 个版本，流程成熟。

---

## 5. 自建的隐性收益：那个"最难的一点"直接消失了

上一轮 AstrBot vs LangBot 争了半天的核心问题是：

> 怎么把飞书 `open_id` **逐请求**传到 MultiRAG，且不经过 prompt？

- LangBot：没有钩子能改后端请求头/体 → 只能 fork
- AstrBot：得写自定义 provider adapter 才行

**自建之后这个问题不存在了。** `feishu_service` 和 `dialog_service` 在同一个进程、
同一个事务上下文里，直接传 user 对象就完事，不需要 HTTP header、不需要 `extra_body`、
不需要序列化再反序列化，也没有中间环节被伪造的风险。

顺带省掉的还有：
- 一次跨服务调用的延迟和失败面
- 一套独立的鉴权/密钥管理（API Token 都不用发了）
- 一份独立的配置、日志、监控、告警
- 一个部署单元

**这是自建最大的价值，比省下的那点部署成本重要得多**——因为它直接作用在项目的核心诉求
（部门数据不外泄）上。

---

## 6. 具体抄 LangBot 的哪几块

LangBot 已重构到 `src/` 目录，路径要现查。重点看这几个 PR 的 diff（含飞书流式卡片的完整演进）：

| PR/Issue | 内容 | 价值 |
|---|---|---|
| [#1437](https://github.com/langbot-app/LangBot/pull/1437)（2025-05） | 首次实现 Dify 消息流式输出，用飞书卡片消息实现消息更新 | 卡片流式的基础实现 |
| [#1442](https://github.com/langbot-app/LangBot/pull/1442) / [#1571](https://github.com/langbot-app/LangBot/pull/1571) | 完善流式输出与 pipeline stream | 流式管线抽象 |
| [#1870](https://github.com/langbot-app/LangBot/issues/1870)（2025-12，已修） | 飞书机器人流式回复无法更新卡片 | **踩坑记录，直接避坑** |
| [#2321](https://github.com/langbot-app/LangBot/pull/2321)（2026-07） | 剥离思维链、按轮次轮换 stream、投递 outbox 媒体 | 生产级细节 |
| [#1959](https://github.com/langbot-app/LangBot/issues/1959)（2026-02，**仍开着**） | 飞书流式输出报错 | 已知未解问题，我们要自己解 |

另外从 **AstrBot 文档**（不是代码）直接抄的事实性内容：

- 完整权限点清单：`im:message`、`im:message:send_as_bot`、`im:resource:upload`、`im:resource`；
  群聊加 `im:message.group_at_msg:readonly`、`im:message.group_msg`；流式加 `cardkit:card:write`
- 消息类型能力边界：收只支持文本 + 图片；语音/视频/文件可发不可收
- 流式前提：飞书客户端 ≥ 7.20

这些是文档里的事实，抄没有任何问题。

---

## 7. 工期

| 路线 | 工期 | 代价 |
|---|---|---|
| 用现成网关（AstrBot/LangBot） | 6~9 周 | 多一个部署单元、多一份配置、长期跟随两个上游、身份透传要绕 |
| **自建（SDK 打底 + 抄 LangBot）** | **7~11 周** | 飞书 API 变更自己跟、卡片 UI 自己设计、管理面板自己加 |

**只多 1~2 周**。考虑到 §5 那些隐性收益，这个价很划算。

拆分（与后端改造并行）：

| 周 | 后端线 | 飞书线 |
|---|---|---|
| 0 | — | ⚠️ 验出网（`open.feishu.cn` + WSS）· 统计公司飞书客户端版本分布 |
| 1–2 | **P0 修 `accessible()` 越权读** | `lark-oapi` 跑通最小闭环：长连接、纯文本、单知识库、无权限 |
| 3–8 | P1 Department / ResourceGrant / 审计 | 流式卡片 + 去重 + 消息类型 + 会话映射（抄 LangBot） |
| 9 | — | 切 Webhook 模式 · 接上权限层 · 灰度 1~2 个部门 |
| 10–12 | — | 全员铺开 · 通讯录同步 · （可选）飞书知识空间连接器 |

---

## 8. 风险与代价（诚实版）

| 风险 | 说明 | 缓解 |
|---|---|---|
| 飞书 API 变更自己跟 | 用现成网关时上游会帮你跟 | 官方 SDK 已挡掉大部分；订阅飞书开放平台变更公告 |
| 多消息类型容易漏 | `post` 富文本是嵌套 JSON、合并转发、图片要走 `im/v1/messages/:id/resources/:file_key` | 第一版只支持 text + post，图片放二期 |
| 卡片 UI 自己设计 | 没有现成模板 | 用飞书卡片搭建工具先出 JSON 2.0 模板 |
| 无管理面板 | 配 bot 要手工写库或加前端 | 一期先用配置文件 + 一个内部 API |
| 门禁 | 新代码进 mypy 纳管范围，`make verify` 必须全绿 | 按 AGENTS.md 走，别改门禁 |
| ⚠️ **出网** | 公司网络拦直连 TLS（历史记录），飞书要出站 HTTPS + WSS | **第 0 周必须先验**，否则后面全是白干 |

---

## 9. 顺手要清理的两处坏样例

仓库里有两个已经打不通的 IM 集成样例，会误导后来人照着抄：

1. `core/svr/discord_svr.py` —— 硬编码配置，打 `/v1/api/completion_aibotk`（当前代码里不存在），
   用了已废弃的 `asyncio.get_event_loop()`
2. `tools/chatgpt-on-wechat/plugins/multirag_chat.py` —— 打 `/v1/api/new_conversation`、
   `/v1/api/completion`（同样已不存在）

建议：要么删，要么在写 `feishu_svr.py` 时顺手改成打
`/api/v1/chats_openai/{chat_id}/chat/completions`，当作正面样例。

---

## 10. 最终建议一句话版

> **用飞书官方 `lark-oapi`（MIT）做协议层，抄 LangBot（Apache-2.0）的流式卡片与产品级细节，
> 权限层自己写；代码落在 `api/apps/feishu_app.py` + `common/im/feishu/` + `core/svr/feishu_svr.py`，
> 生产走 Webhook、开发走长连接；AstrBot 只读不抄。**
>
> **但第 1 优先级不是飞书，是 P0 那个越权读**——飞书接上去只会把这个洞暴露给全公司。

---

## 附：本文新增引用

- [larksuite/oapi-sdk-python（官方 Python SDK，MIT）](https://github.com/larksuite/oapi-sdk-python) · [README.zh](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/README.zh.md) · [PyPI lark-oapi](https://pypi.org/project/lark-oapi/)
- [使用长连接接收事件（官方）](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)
- [AstrBot 飞书接入文档（权限点/消息类型参考）](https://docs.astrbot.app/platform/lark.html)
- [LangBot 仓库](https://github.com/langbot-app/LangBot)
