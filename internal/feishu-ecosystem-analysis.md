# 飞书 AI 生态调研 —— 能借多少力，借不到什么

> 生成日期：2026-07-28
> 配套文档：[多租户评估](multitenancy-and-feishu-report.md) · **[最终落地方案](feishu-inhouse-plan.md)**
> 本文是本地评审笔记（`internal/*.md`，不入库）
>
> **⚠️ 本文 §3.3 推荐 AstrBot，前提是"部署现成网关"。**
> 若前提改为"把代码集成进 MultiRAG 自身"，结论翻转（AGPL 代码不能进 Apache-2.0 仓库，
> 且自建后身份透传难点消失）——**以 [feishu-inhouse-plan.md](feishu-inhouse-plan.md) 为准**。

上一版报告假设"从零自研飞书网关"。这一版调研了生态现状后，**结论要修正**：协议层大部分
可以借力，但**权限层一点也借不到**，而权限层恰好是我们的核心诉求。

---

## 0. 先厘清一个方向问题

生态里"飞书 + AI"的项目其实是**两个方向相反的类别**，很容易混为一谈：

```
A 类：AI → 飞书          B 类：飞书 → AI
让 AI 去读写飞书的        让飞书里的人向 AI 提问，
文档/日历/多维表格         AI 查知识库后回答

· 飞书官方 OpenClaw 插件   · LangBot
· lark-openapi-mcp        · AstrBot
· 各种 feishu-mcp         · dify-on-lark / FeishuRBT
                          · chatgpt-on-wechat
```

**你的需求（全公司在飞书用机器人问知识库）是 B 类。飞书官方给 OpenClaw 做的插件是 A 类。**

两者不互斥，但解决的不是同一个问题。而 MultiRAG 巧的是**两边都已经有半成品**：

| 方向 | 现有资产 | 状态 |
|---|---|---|
| A（MultiRAG 调飞书） | `MCPServer` 表（`api/db/db_models.py:1492-1503`，含 url/server_type/headers/variables，按租户隔离）+ `mcp/client/` | ✅ 可用，挂 `lark-openapi-mcp` 即可 |
| B（飞书调 MultiRAG） | `mcp/server/server.py`（FastMCP，已暴露 `list_datasets` / `multirag_retrieval`，Bearer 鉴权，streamable HTTP + SSE） | ✅ 可用 |
| B（IM 插件先例） | `tools/chatgpt-on-wechat/plugins/multirag_chat.py` | ⚠️ 打的是 RAGFlow 时代的 `/v1/api/new_conversation`、`/v1/api/completion`，**现在的代码里已经没有这两个端点了，这个插件是坏的** |

---

## 1. 飞书官方 OpenClaw 插件 —— 深挖，以及为什么它不能直接用

你提到的就是这个：飞书官方发布了 OpenClaw 插件（不是社区做的），有专门的
[方案中心页](https://www.feishu.cn/openclaw)。

### 1.1 它是什么

```bash
npx -y @larksuite/openclaw-lark install     # 安装
/feishu auth                                 # 在飞书里发这条命令完成批量授权
/feishu start                                # 验证
/feishu doctor                               # 排障
```

能力覆盖面确实很宽：

| 类别 | 功能 |
|---|---|
| 消息 | 读群聊/单聊历史、话题回复、发消息、搜索、图片/文件下载、识别合并转发消息、发表情 |
| 文档 | 创建云文档、更新内容、读取文档 |
| 多维表格 | 表格/字段/记录/视图的增删改查、批量操作、高级筛选 |
| 日历 | 创建/查询/修改/删除日程、参会人管理、忙闲查询 |
| 任务 | 任务、清单、子任务、评论 |
| 其他 | 流式输出卡片回复（需手动开启） |

**授权模型是关键差异**：它走**用户身份授权**（OAuth → `user_access_token`），OpenClaw
"以你的身份"操作飞书。这跟大部分第三方方案用应用身份（`tenant_access_token`）不同，
也是它宣称"解决了第三方插件权限授权繁琐"的地方。

### 1.2 为什么不能拿来当全员知识库机器人

四条，任何一条都足够：

1. **方向不对**。它让 OpenClaw 去读你的飞书文档，不是让飞书用户来问 MultiRAG 的知识库。
   要做后者，还是得自己接 `im.message.receive_v1`。

2. **定位是个人助理，一实例一主人**。OpenClaw 是 local-first、跑在你自己机器上的私人
   Agent。全公司几百人共用一个实例，不是它的设计目标，也没有租户隔离。

3. **飞书官方自己不建议接生产**。原文大意是：插件处于快速迭代阶段，建议先拿个人账号
   安全地玩起来，等后续安全隔离能力更成熟了，再考虑接入真实工作环境。
   ——**这句话出自飞书官方文档，不是我的判断**。企业级部署要慎重。

4. **`im:message.send_as_user` 部分企业不支持**（官方文档明确点名字节自己就不支持）。
   代发消息这类能力在很多企业的 IT 策略下会被卡掉。

### 1.3 但它给了一个有价值的启发

"以用户身份"这个模型，正好对应我们第一部分报告里的 **R2（APIToken 绑租户不绑人）**。
飞书官方选择了 user_access_token 而不是 tenant_access_token，说明**在企业 AI 场景里，
"AI 代表谁行动"是必须显式解决的问题，绕不过去**。我们的场景是反向的同一个问题：
"飞书用户在 MultiRAG 里代表谁"。

---

## 2. lark-openapi-mcp（飞书官方 MCP）

仓库：[larksuite/lark-openapi-mcp](https://github.com/larksuite/lark-openapi-mcp)

```bash
npx -y @larksuiteoapi/lark-mcp mcp -a <app_id> -s <app_secret>
```

- **双鉴权模式**：应用身份（app_id/secret → tenant_access_token）/ 用户身份
  （`--oauth --token-mode user_access_token`，需先 `login`，默认回调
  `http://localhost:3000/callback`）
- **工具集可裁剪**：`-t "im.v1.message.create,preset.calendar.default"`，支持 preset
- **附带 `recall-developer-documents` 工具**：能检索飞书开放平台全量开发文档 —— 这个对我们
  开发期很有用，接飞书时可以让 Agent 自己查文档
- **国际版**：`--domain https://open.larksuite.com`

**已知限制（官方列的）**：
- 不支持文件上传下载
- 不支持直接编辑云文档（只能导入和读取）
- Beta 版本，API 可能变
- 非 preset 的 API 没做兼容性测试，AI 理解效果可能不好

**对我们的价值**：MultiRAG 的 Agent 可以直接挂它——`MCPServer` 表已经支持按租户配 MCP
server（url + headers + variables）。挂上之后 Agent 就能"查飞书文档""发飞书消息""建日程"。

**这是纯增量能力，跟部门权限隔离一点关系都没有**，别指望它解决第一部分的问题。

---

## 3. B 类网关横评 —— 这才是我们真正要选的东西

| 项目 | Star（撰写时） | License | 飞书 | 后端接法 | 流式卡片 | 身份透传 |
|---|---:|---|---|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 38.2k | **AGPL-3.0** | ✅ 原生 | OpenAI 兼容（可自定义 base_url） | 部分 | ❌ |
| [LangBot](https://github.com/langbot-app/LangBot) | 17.1k | Apache-2.0 | ✅ 原生 | OpenAI 兼容 / Dify / n8n / Coze / Langflow | 社区在做（[#1814 类似issue](https://github.com/ThinkInAIXYZ/deepchat/issues/1814)） | ❌ |
| dify-on-lark | 小 | — | ✅ | 仅 Dify | ✅ 有 AI 卡片打字机 | ❌ |
| [FeishuRBT](https://github.com/anycodes/FeishuRBT) | 小 | — | ✅ | 仅 Dify | — | ❌ |
| chatgpt-on-wechat | 大 | MIT | ✅ | 插件自定义 | ❌ | ❌ |
| 自研 gateway | — | — | 自己写 | 任意 | 自己写 | ✅ 想怎样都行 |

### 3.1 最重要的一条结论

**这 5 个开源网关，没有一个解决"按飞书用户身份决定他能检索哪些知识库"。**

它们的模型统一是：**一个 bot = 一个后端 API Key = 一份固定的可见范围**。

- LangBot 确实有"多用户管理和访问控制"，但那是 **LangBot 自己的**用户体系（谁能用这个 bot），
  不是把飞书用户身份透传到后端让后端做数据级授权。
- AstrBot 同理。

所以：

> **生态能帮我们省掉飞书协议层（事件订阅、去重、卡片流式、限流、多消息类型）约 60~70% 的
> 工作量，但权限层一点都帮不上。权限层必须我们自己在 MultiRAG 里做——也就是上一版报告的
> P0 + P1，一步都省不掉。**

### 3.2 选哪个（**若 License 是硬约束**时的答案）

**推荐 LangBot**，理由：

1. **Apache-2.0**。AstrBot 是 AGPL-3.0 —— 公司内部自用没问题，但如果以后要把改造后的
   网关随 MultiRAG 一起交付/分发，AGPL 的传染性会是法务问题。MultiRAG 自己是 Apache-2.0，
   混 AGPL 代码进来要谨慎。
2. **原生支持 OpenAI 兼容后端**，MultiRAG 的 `/api/v1/chats_openai/{chat_id}/chat/completions`
   （`api/apps/sdk/session.py:280`）可以直接当后端配上去，**零后端改造就能跑通第一版**。
3. 飞书接入配置很轻：
   ```json
   {"adapter": "lark", "enable": true, "app_id": "cli_xxx", "app_secret": "xxx",
    "bot_name": "...", "enable-webhook": false, "port": 2285, "encrypt-key": "xxx"}
   ```
   长连接/Webhook 两种模式都支持（国际版飞书必须 Webhook）。
4. 插件系统成熟，我们要加的"把飞书 open_id 塞进请求头"是个小插件的量。

**要验证的点（PoC 第一件事）**：LangBot 的插件/pipeline 扩展点能不能在调后端时**自定义
HTTP header**。如果不能，就得 fork 一个小改动，或者退回自研 gateway。**这个决定后面所有
工期，务必先验。**

> **§3.3 是对 AstrBot vs LangBot 的深挖，结论与本节的初步倾向不同——以 §3.3 为准。**

### 3.3 AstrBot vs LangBot 深度对比（不考虑 License 的前提下）

#### 硬数据（2026-07-28 从 GitHub API 取）

| 指标 | AstrBot | LangBot |
|---|---:|---:|
| Star | **38,230** | 17,148 |
| Fork | 2,716 | 1,520 |
| Watcher | 80 | **162** |
| Open issues | **1,333** | 123 |
| Open issues / Star | **3.5%** | **0.7%** |
| License | AGPL-3.0 | Apache-2.0 |
| 最近推送 | 2026-07-28 | 2026-07-27 |

两个都活跃。AstrBot 用户基数明显更大（Star 2.2×、Fork 1.8×），但 open issue 是
LangBot 的 **10.8 倍**，issue/star 比是 5 倍。这**不必然等于质量差**——用户多自然 issue 多——
但意味着我们提的 bug 大概率排不上队，得做好自己 fork 修的准备。
反过来 LangBot 的 watcher 比 AstrBot 还多一倍（162 vs 80），说明盯着它的偏"要拿它干活"
的人，而不是"围观"的人。

#### 飞书适配器

| 维度 | AstrBot | LangBot |
|---|---|---|
| 流式卡片 | ✅ 原生，文档明确：需 `cardkit:card:write`，客户端 ≥7.20 | ✅ 已实现（PR #1437 2025-05 首发，#1442/#1571 完善，#1870 修卡片更新，#2321 2026-07 修思维链泄漏） |
| 流式已知问题 | — | [#1959 飞书流式输出报错](https://github.com/langbot-app/LangBot/issues/1959)（2026-02 开着） |
| 事件订阅 | `socket`（长连接）/ `webhook` 可选 | 长连接（默认）/ Webhook |
| 建 bot 便利性 | v4.25.0+ 支持**扫码自动建 bot**，配置自动写入 | 手工填 app_id/app_secret |
| 消息类型 | 收：**文本 + 图片**；发：可发语音/视频/文件。**明确不支持接收语音/视频/文件** | 文档未详列 |
| 权限点文档 | ✅ 列得很全：`im:message`、`im:message:send_as_bot`、`im:resource:upload`、`im:resource`；群聊加 `im:message.group_at_msg:readonly`、`im:message.group_msg`；流式加 `cardkit:card:write` | 文档只说"添加图中所示权限" |
| 社区增强 | `astrbot_plugin_lark_enhance`：补群名/历史/引用回复/@群成员 | — |

**飞书适配器这一栏 AstrBot 更成熟**，尤其权限点和消息类型限制都写清楚了，能少踩坑。

#### 关键差异：身份透传能力（我们最难的那一点）

这才是决定性的。我们要做的是把飞书 `open_id` **逐请求**传到 MultiRAG，且**不能走 prompt**
（走 prompt 等于 LLM 可见、用户可伪造、可被 prompt injection 篡改，权限系统直接失效）。

**AstrBot**：

- OpenAI provider 支持 `custom_headers` 和 `custom_extra_body`（`astrbot/core/provider/sources/openai_source.py`）：
  ```python
  self.custom_headers = provider_config.get("custom_headers", {})
  self.client = AsyncOpenAI(api_key=..., base_url=..., default_headers=self.custom_headers, ...)
  ```
  ⚠️ 但这是**provider 级静态配置**，不是逐请求的。
- `ProviderRequest`（`astrbot/core/provider/entities.py`）字段是
  `prompt / session_id / image_urls / audio_urls / extra_user_content_parts / func_tool /
  contexts / system_prompt / conversation / tool_calls_result / model`
  ——**没有 `extra_body` 也没有 `extra_headers`**，所以 `@filter.on_llm_request()` 也改不了请求头。
- ⭐ **但有真正的逃生舱**：`@register_provider_adapter("...")` 可以**注册完全自定义的 Provider**
  （官方 provider 自己就是这么注册的）。我们写一个 `multirag_provider`，对打给 MultiRAG 的
  HTTP 请求有 100% 控制权。
- `event.message_obj.raw_message` 能拿到平台原始消息，飞书 `open_id` 在里面。

**LangBot**：

- 插件事件是 pipeline 级：`*MessageReceived`、`*NormalMessageReceived`、
  `PromptPreProcessing`、`NormalMessageResponded`
- 可改的字段：`user_message_alter`（消息文本）、`default_prompt`（系统提示）、
  `prompt`（历史）、`reply_message_chain`（回复）
- **没有任何一个钩子能改发给后端的 HTTP header 或请求体**

→ 结论：**在"逐请求注入身份"这一点上，AstrBot 有干净解，LangBot 没有**，只能 fork requester。

#### 但两者其实都有一条更干脆的路

写一个插件，在**消息层直接接管**——自己调 MultiRAG、自己返回结果，完全绕过框架的
provider/LLM 层：

- AstrBot：`@filter.event_message_type()` 处理器 + `yield event.plain_result(...)`
- LangBot：`*NormalMessageReceived` handler + 设 `reply_message_chain` + 阻断默认流程

走这条路，两者差距从"能不能"变成"顺不顺手"。

⚠️ **代价要实测**：绕过 provider 层，可能连框架自带的流式卡片一起绕过去了。
这是 PoC 必须验的第一件事。

#### 定位差异 —— 长期成本的真正来源

| | AstrBot | LangBot |
|---|---|---|
| 自我定位 | "一站式 Agent 聊天机器人平台"，自带知识库、Agent、MCP、Skills | "生产级多平台智能机器人开发平台" |
| 想扮演的角色 | **大脑**（它自己就是 Agent） | **嘴巴**（前置 Dify / n8n / Langflow / Coze 这类外部 AI 应用） |
| 企业向功能 | 相对少 | 限流、敏感词过滤、监控告警、多用户与访问控制、Web 面板、k8s |
| 工程规范信号 | — | `ARCHITECTURE.md`、CLA、codecov、pytest、`AGENTS.md`、`Makefile` |

**我们已经有大脑了（MultiRAG），需要的是嘴巴。**

用 AstrBot 意味着：常年只用它 ~20% 的功能（平台适配 + 消息编排），却要跟着它另外 80%
（知识库、Agent 引擎、Skills、MCP）一起升级、一起吃它们的 breaking change。
LangBot 的产品定位跟我们的用法完全一致，长期跟随成本更低。

#### 综合结论

**如果只看"接飞书这件事本身"：AstrBot 更强**（适配器更成熟、扩展点更深、文档更细）。

**如果看"三年后谁更省心"：LangBot 更合身**（定位一致、企业向功能齐、issue 背压小、
工程规范好）。

**我的建议：优先 AstrBot，但把它当"飞书协议适配器"用，不要用它的大脑。**
理由是我们的核心难点（逐请求身份透传）在 AstrBot 里有干净解，在 LangBot 里只能 fork。
选型的权重应该压在"能不能干净地做对权限"上，而不是压在"框架有多少功能"上——
功能我们有的是，权限做不对整个项目就没法上线。

#### "两者综合"可行吗？

**不建议。** 同一个飞书自建应用的事件订阅只能指向一处（长连接还是集群不广播的），
两个网关同时接会打架。要综合只能是"用 A 的代码 + 抄 B 的设计"，而 B 那些企业向能力
（限流、审计、访问控制）我们本来就要在 MultiRAG 侧做一遍——抄它没意义。

#### 第 0 周实测清单（2~3 天，结论比任何 star 数都有说服力）

两个都装上，连**同一个飞书测试应用**（分时切换，别同时开），各跑一遍，只验三件事：

1. **身份能不能逐请求传到后端，且不经过 prompt** —— 这条一票否决
2. **流式卡片在你们公司实际的飞书客户端版本上什么效果** —— 先统计一下版本分布，
   低于 7.20 的占比多少
3. **富文本（post）、图片消息的处理质量** —— AstrBot 明确不收语音/视频/文件，
   LangBot 未文档化，都得实测

> 附：MultiRAG 侧已有的两个可用挂载点，实测时能省事——
> `ChatCompletionOpenAIRequest` 有 **`extra_body`** 字段（`api/apps/sdk/session.py:84-90`），
> `AgentCompletionRequest` 更是已经有 **`user_id`** 和 **`custom_header`** 字段
> （`api/apps/sdk/session.py:93-107`）。也就是说身份透传**未必非要走 HTTP header**，
> 走 `extra_body` 或 Agent 端点的 `user_id` 可能更省事——这会让 LangBot 的劣势缩小，
> 因为它的 requester 可能允许配置额外的请求体字段。**这一条也放进第 0 周一起验。**

> **License 备注**（你说不考虑，这里只留一行事实）：AGPL-3.0 的传染在"分发"和"网络交互"
> 时触发。纯公司内部员工使用、不对外提供服务、不分发修改版，一般不构成对公众分发，风险很低。
> 但如果哪天要把它跟 MultiRAG 打包卖给客户、或做成对外 SaaS，就会咬人。**做个记录即可。**

---

## 4. 修正后的推荐架构

```
飞书                LangBot（网关，开源）              MultiRAG
────────           ──────────────────────           ──────────────
用户 @机器人  ──►   · im.message.receive_v1
                    · message_id 幂等去重
                    · 群聊/私聊、富文本、图片
                    · CardKit 流式卡片回写
                    · 限流/重试
                    ·〔我们加的插件〕
                       open_id ─► 内部 user_id
                       注入 X-End-User-Id  ──────►  · 解析 end_user
                                                    · 算 visible_kb_ids
                                                    · ∩ dialog.kb_ids
                                                    · 只在交集里检索
                                                    · 写审计日志
                                              ◄──── SSE 流式返回
```

对应改造：

| 侧 | 改动 | 出处 |
|---|---|---|
| LangBot | 一个插件：`open_id` → 内部 user 映射 + 注入 header | 新写，约 200~400 行 |
| MultiRAG | 认 `X-End-User-Id`，算可见范围 | 第一部分 P1 |
| MultiRAG | `Department` / `ResourceGrant` / 审计表 | 第一部分 P1 |
| MultiRAG | 修 `accessible()` 越权读 | 第一部分 **P0，必做** |

---

## 5. 一个必须提前想清楚的问题：飞书 Aily

飞书自己有 **Aily（智能伙伴创建平台）** —— 企业级 Agent 开发平台，能力包括：

- 关联飞书知识空间，**内容自动更新**
- **企业级权限管控**（这正是我们要花 6 周做的东西，人家是内建的）
- 支持 MCP 服务接入、AI 编排引擎
- 支持**本地代理服务**，可以让 HTTP 请求/自建连接器访问企业内网
- 能操作飞书文档、多维表格、任务

**这是 MultiRAG 在这个场景下的直接竞品，而且它天然在飞书里。** 项目推给业务方的时候，
大概率会被问："我们已经有飞书了，为什么不用 Aily？"

建议提前准备好差异化说明。MultiRAG 站得住的点：

| 维度 | MultiRAG | Aily |
|---|---|---|
| 文档理解深度 | deepdoc：版面分析、表格结构还原、OCR、公式 | 通用解析 |
| 部署形态 | 完全私有化，数据不出内网 | 飞书云（企业版有私有化选项但成本高） |
| 模型自主 | 任意 provider / 本地模型，租户级配置 | 平台托管为主 |
| 高级 RAG | GraphRAG、RAPTOR、思维导图、多路召回融合、rerank | 相对黑盒 |
| 非飞书数据源 | 25+ 连接器（SharePoint/Confluence/Jira/GitLab/S3/RDBMS…） | 以飞书生态为主 |
| 成本 | 自建 | 按席位/调用计费 |

**同时，Aily 的"代理通道"其实提供了第四种接入形态**：让 Aily 当飞书侧的门面，通过代理
通道调 MultiRAG 的 `/api/v1/retrieval` 或 MCP。这样权限管控由 Aily 负责，我们只当检索引擎。
适合"公司已经在推 Aily"的情况——但代价是我们对权限逻辑失去控制，且强绑飞书。

---

## 6. 四种形态终评

| 形态 | 做法 | 工期 | 权限能力 | 风险 |
|---|---|---|---|---|
| **A. 自研 gateway** | 上一版报告的方案 | 10~15 周 | 完全可控 | 全部自己扛 |
| **B. 开源网关 + MultiRAG 改造** ⭐ | 网关用开源，权限自己做 | **6~9 周** | 完全可控 | 依赖第三方扩展点（需先验证） |
| ↳ B1. **AstrBot**（内部自用，不看 License） | 只当协议适配器，不用它的大脑 | 同上 | 同上 | 1333 open issue 背压；定位冲突要长期忍 |
| ↳ B2. **LangBot**（License 是硬约束时） | 同上 | 同上 | 需 fork requester 才能透传身份 | fork 后跟随上游成本 |
| **C. 飞书官方 OpenClaw 插件** | 直接用 | 1 周 | ❌ 无 | 定位是个人助理；**官方明确不建议接生产** |
| **D. Aily 代理通道调 MultiRAG** | 我们只当检索引擎 | 3~4 周 | 交给 Aily | 强绑飞书；失去权限控制权；依赖公司飞书版本 |

**推荐 B**。相比上一版报告，工期从 10~15 周压到 **6~9 周**，省下来的正好是飞书协议层
（事件、去重、卡片、限流、多消息类型）那 3~4 周。

**但 P0（修越权读）+ P1（组织化 + `X-End-User-Id`）一天都省不掉**——这是生态帮不上的部分，
也恰恰是"部门数据不外泄"这个核心诉求的全部内容。

---

## 7. 生态里踩过的坑（别人的，我们可以免费吸取）

1. **CardKit v1 / v2 版本混乱**。多个项目撞过同一个墙：`@larksuiteoapi/node-sdk` 里
   没有 `cardkit.v2` 模块，但飞书官方 CardKit **v1 API 本身就支持 JSON 2.0 schema 和
   `streaming_mode`**。见 [Claude-to-IM-skill#76](https://github.com/op7418/Claude-to-IM-skill/issues/76)、
   [deepchat#1814](https://github.com/ThinkInAIXYZ/deepchat/issues/1814)、
   [QwenPaw#3001](https://github.com/agentscope-ai/QwenPaw/issues/3001)。
   → **别去找 v2，用 v1 + JSON 2.0**。

2. **官方 OpenClaw 插件版本迭代极快**，安装命令都带死版本号
   （`--version 2026.4.7 --tools-version 1.0.37`）。如果真要用，**必须锁版本**，
   不然某天自动更新就炸。

3. **AGPL 传染**。AstrBot 星最多（38.2k）但 AGPL-3.0。选它之前先过法务。

4. **权限点要一次申请齐**。官方 OpenClaw 插件文档里列了一长串权限点（`im:message:readonly`、
   `im:message:send_as_bot`、`docx:document:readonly`、`docx:document:write_only`、
   `base:record:create/update`、`calendar:calendar.event:create`…）并提供**批量导入**。
   我们也照做——飞书开放平台支持权限批量导入，别一个个点，也别挤牙膏式多次送审。

5. **仓库里那个 `multirag_chat.py` 是坏的**。`tools/chatgpt-on-wechat/plugins/multirag_chat.py`
   打的 `/v1/api/new_conversation`、`/v1/api/completion` 在当前代码里已不存在。
   要么删掉，要么改成打 `/api/v1/chats_openai/{chat_id}/chat/completions`。留着会误导人。

---

## 8. 修订后的执行计划

```
第 0 周   ⚠️ 三件事必须先做，否则后面全是白干（见 §3.3 实测清单）：
          1. 验证出网：能不能访问 open.feishu.cn / WSS 长连接（公司网络拦直连 TLS）
          2. AstrBot / LangBot 各跑一遍，验身份能否逐请求传到后端（不经 prompt）
          3. 统计公司飞书客户端版本分布（<7.20 的占比决定流式卡片方案）
          ↓
第 1-2 周  后端 P0（修 accessible 越权读）  ∥  选定的网关接飞书跑通（用现成 OpenAI 兼容端点，
                                                单个知识库，不谈权限）
          ↓
第 3-8 周  后端 P1（Department/Grant/审计/X-End-User-Id）  ∥  网关插件（身份映射）
                                                            ∥  流式卡片调优
          ↓
第 9 周    交汇：切到"按人授权"形态，灰度到 1~2 个部门
          ↓
第 10-12 周 全员铺开 + 通讯录同步（P3）+ 飞书知识空间连接器（P4，可选）
```

**第 0 周那两个验证，建议这周就做掉**，成本几乎为零，但结论会决定后面 3 个月的走法。

---

## 附：本文引用来源

飞书官方：
- [飞书 × OpenClaw 接入方案中心](https://www.feishu.cn/openclaw)
- [OpenClaw 飞书官方插件上线｜功能、安装更新教程与常见问题](https://www.feishu.cn/content/article/7613711414611463386)
- [飞书 × OpenClaw 部署与应用分类页](https://www.feishu.cn/content/topic/openclaw)
- [larksuite/lark-openapi-mcp（官方 OpenAPI MCP）](https://github.com/larksuite/lark-openapi-mcp)
- [lark-openapi-mcp README_ZH](https://github.com/larksuite/lark-openapi-mcp/blob/main/README_ZH.md)
- [飞书 aily：企业级智能体开发平台](https://www.feishu.cn/content/3d5z9ttt)
- [飞书 aily 智能体定制能力升级](https://www.feishu.cn/content/article/7631864469689240764)
- [飞书智能伙伴创建平台 Aily 最新版功能介绍](https://www.feishu.cn/content/ap8ie3h2)

开源项目：
- [langbot-app/LangBot](https://github.com/langbot-app/LangBot) · [飞书接入文档](https://v3.docs.langbot.app/deploy/platforms/lark)
- [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) · [文档](https://docs.astrbot.app/what-is-astrbot.html)
- [anycodes/FeishuRBT（Dify 飞书连接器）](https://github.com/anycodes/FeishuRBT)
- [dify-on-lark](https://gitee.com/duhongming/dify-on-lark)
- [Dify 官方：通过 LangBot 接入 IM 平台](https://docs.dify.ai/zh-hans/learn-more/use-cases/connect-dify-to-various-im-platforms-by-using-langbot)
- [BytePioneer-AI/openclaw-china（社区中国 IM 渠道扩展）](https://github.com/BytePioneer-AI/openclaw-china)
- [AlexAnys/openclaw-feishu（社区配置指南）](https://github.com/AlexAnys/openclaw-feishu)
- [Cheerwhy/hermes-lark-streaming（CardKit 流式卡片插件）](https://github.com/Cheerwhy/hermes-lark-streaming)

生态踩坑记录：
- [Claude-to-IM-skill#76：cardkit.v2 在 node-sdk 中不存在](https://github.com/op7418/Claude-to-IM-skill/issues/76)
- [deepchat#1814：飞书/Lark 流式卡片支持](https://github.com/ThinkInAIXYZ/deepchat/issues/1814)
- [QwenPaw#3001：支持飞书 CardKit 流式输出](https://github.com/agentscope-ai/QwenPaw/issues/3001)
