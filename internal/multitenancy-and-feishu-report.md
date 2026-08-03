# MultiRAG 多租户能力评估 + 飞书接入方案

> 生成日期：2026-07-28
> 代码基线：`main` @ 39a21638
> 本文是本地评审笔记（`internal/*.md`，不入库）
>
> **⚠️ 两处已被后续调研修正，以配套文档为准：**
> - **第一部分 R1 / P0**：那个越权读是 RAGFlow 上游已修的 [#14645](https://github.com/infiniflow/ragflow/pull/14645)（2026-05-09），
>   我们只是没跟；**受影响函数是 4 个不是 2 个**；修法应"跟上游"而非"自建 authz 层"。
>   → [ragflow-upstream-followup.md](ragflow-upstream-followup.md)
> - **第二部分形态与工期**：→ [feishu-ecosystem-analysis.md](feishu-ecosystem-analysis.md)
>   → 最终落地方案 [feishu-inhouse-plan.md](feishu-inhouse-plan.md)

---

# 第一部分：多租户能力评估

## 1. 现状：现有模型到底是什么

### 1.1 租户 = 个人，不是组织

这是最关键的一条事实。看 `api/db/joint_services/user_account_service.py:68-89`：

```python
user_id = uuid.uuid1().hex
tenant = {"id": user_id, "name": user_info["nickname"] + "'s Kingdom", ...}
usr_tenant = {"tenant_id": user_id, "user_id": user_id, "role": UserTenantRole.OWNER}
```

**每注册一个用户就自动建一个同 ID 的租户**（`tenant.id == user.id`）。这是从 RAGFlow 继承来的
"个人王国 + 邀请制"模型：租户不是一个独立的组织实体，而是某个人的个人空间；所谓"团队"
是别人被邀请进你的个人空间。

含义：
- 没有"部门"这个一等公民，也没有组织架构表
- 租户的所有权绑死在一个自然人账号上，这个人离职 = 部门空间的 owner 悬空
- 一个人可以属于多个租户（`t_ai_user_tenants` 多行），但**没有"当前租户/当前部门"的上下文概念**

### 1.2 角色只有 4 个，全局生效

`api/db/__init__.py:4-13`：

```python
class UserTenantRole(StrEnum):
    OWNER = "owner"; ADMIN = "admin"; NORMAL = "normal"; INVITE = "invite"

class TenantPermission(StrEnum):
    ME = "me"; TEAM = "team"
```

能力矩阵在 `api/db/services/user_service.py:264-285`：

| 能力 | owner | admin | normal | invite |
|---|:---:|:---:|:---:|:---:|
| `can_access_tenant_resources` | ✅ | ✅ | ✅ | ❌ |
| `can_update_tenant_resources` | ✅ | ✅ | ❌ | ❌ |
| `can_manage_members` | ✅ | ✅ | ❌ | ❌ |
| `can_manage_roles` | ✅ | ❌ | ❌ | ❌ |
| `can_download` | ✅ | ✅ | ❌ | ❌ |

角色是**租户级**的，不是资源级的。你没法说"张三对 A 库可写、对 B 库只读"。

### 1.3 资源可见性只有二值开关

`Knowledgebase.permission`（`api/db/db_models.py:1040`）只有 `me | team`，Agent（`UserCanvas.permission`）
同理。没有 per-KB 的成员列表、没有资源级 ACL、没有文档级/chunk 级权限。

### 1.4 物理隔离层：这块其实做得不错

向量集合按 **租户 + 库名** 分（`core/nlp/search.py:70-71`）：

```python
def index_name_one(uid, kb_name):
    return f"multirag_{uid}_{kb_name}"
```

也就是说 Milvus/ES 里不同租户的数据落在**不同 collection/index**，不是靠一个 `tenant_id` 字段
过滤。这是比"逻辑隔离"更强的一档，是本项目多租户里最扎实的部分——**部门间的硬隔离，只要
按"部门=租户"划分，物理层是站得住的**。

存储侧也是：MinIO bucket 按 `kb_id` 分（见 `user_account_service.py:206`），LLM 凭据按租户分
（`TenantLLM`，唯一约束 `tenant_id + llm_factory + llm_name`）。

### 1.5 API 侧身份：token 绑租户，不绑人

`api/db/db_models.py:1209-1219`：

```python
class APIToken(BaseModel):
    tenant_id: Mapped[str] = mapped_column(..., primary_key=True)
    token: ...; beta: ...; dialog_id: ...; source: ...  # none|agent|dialog
```

`token_required` / `beta_token_required`（`api/utils/api_utils.py:249,285`）返回的是 **tenant_id**，
下游一律以 tenant_id 当"当前用户"用。**这条直接决定了第二部分飞书方案的形态**——见 §2.3。

---

## 2. 用它撑"部门私有数据互不可见"，够不够？

分三种落地方式讨论。

### 方案 A：一个部门 = 一个租户 ✅ 可行，是当前唯一能立刻上线的方案

- 部门间隔离：**成立**。向量库物理分 collection，DB 查询全部带 `tenant_id`，跨租户拿不到东西。
- 部门内共享：把库设成 `permission=team`，部门成员都能看。

**但有 5 个真实的坑：**

1. **租户绑个人账号**。租户名硬编码成 `"XX's Kingdom"`，owner 是创建人。部门负责人离职后
   要转移 owner——`tenant_app.py` 有换 owner 的逻辑，但 `tenant.id` 永远等于最初那个人的
   `user.id`，数据里会留下一个"幽灵用户 ID 当部门 ID"的历史包袱。

2. **跨部门共享做不了**。财务的一份制度文档要给全公司看，只能：复制一份到"全员租户"，
   或者把全公司都拉进财务租户（那部门隔离就没了）。**没有中间态**。

3. **一人多部门体验崩坏**。用户同时在研发和产品两个租户，KB 列表接口
   （`knowledgebase_service.py:107`）会把两个租户的 team 库**混在一起返回**，没有部门维度
   分组，也没有"切换当前部门"。用户数一多，列表会很难看。

4. **模型配置要配 N 份**。每个部门一个租户 = 每个部门一套 `TenantLLM`，API Key、base_url
   全要重复配。20 个部门就是 20 份。运维负担线性增长，且改一次模型要改 20 处。

5. **"部门内"仍然是全有全无**。部门内某些资料只想给主管看？做不到——见方案 B 的洞。

### 方案 B：全公司一个租户 + 靠 `permission` 区分 ❌ 不行，现在就有越权读

这是最容易被想到的省事方案，但**代码里 `permission` 只在"列表查询"生效，"按 ID 直取"
完全不校验**。

正确的检查函数是 `api/common/check_team_permission.py:25-37`，它会看 `permission`：

```python
if kb_tenant_id == other: return True
if kb["permission"] != TenantPermission.TEAM: return False   # ← 有校验
```

但**绝大多数端点用的不是它**，而是 `accessible()`：

`api/db/services/knowledgebase_service.py:512-525`
```python
@classmethod
def accessible(cls, db, kb_id, user_id):
    tenant_id = ...  # 取该 KB 的 tenant_id
    membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
    if not membership: return False
    return UserTenantService.can_access_tenant_resources(membership.role)   # ← 不看 permission
```

`api/db/services/document_service.py:2542-2560` 是同样的写法。

具体越权点（我核对过的）：

| 端点 | 位置 | 检查方式 | 是否看 `permission` |
|---|---|---|:---:|
| `GET /kb/detail?kb_id=` | `api/apps/kb_app.py:309-317` | 遍历用户所有租户看 KB 是否属于其一 | ❌ |
| `POST /chunk/retrieval_test` | `api/apps/chunk_app.py:1581-1587` | 同上 | ❌ |
| `GET /kb/{kb_id}/tags` 等一系列 | `api/apps/kb_app.py:453,466,477,493,513,546,568,613,1614` | `KnowledgebaseService.accessible` | ❌ |
| `/api/v1/datasets/*` (SDK) | `api/apps/services/dataset_api_service.py:428,463,485,548,570,632` | 同上 | ❌ |
| `/document/*` 十余个端点 | `api/apps/document_app.py:1683,1945,2009,...` | `DocumentService.accessible` | ❌ |
| 文档上传/改名 | `api/apps/document_app.py:555,748` | `check_kb_team_permission` | ✅ |

**结论**：同一租户内，任何 `normal` 成员只要知道 `kb_id`，就能读到标记为 `permission="me"`
的私有库的详情、标签、乃至通过 `retrieval_test` 检索到里面的 chunk 原文。列表接口看不到，
但按 ID 直取拿得到——典型的 IDOR。

> 这不是"设计上不支持"，是**同一个语义有两套实现且不一致**。属于要修的 bug，不是要加的功能。

### 方案 C：改造后的模型 ✅ 推荐的目标态

见 §4 路线图。

### 2.1 还缺什么（部门场景的硬需求 vs 现状）

| 需求 | 现状 | 缺口 |
|---|---|---|
| 部门间数据不可见 | ✅ 方案 A 可满足 | — |
| 部门内分级（主管/员工） | ❌ | 无资源级角色 |
| 跨部门定向共享单个库 | ❌ | 无资源级授权表 |
| 一人多部门 + 部门上下文 | ⚠️ 能属于多个，但无上下文切换 | UI/API 都缺 |
| 文档级权限 | ❌ | 权限最小粒度是 KB |
| chunk 级权限（同一库内分密级） | ❌ | 向量库无 ACL 字段 |
| 组织架构自动同步 | ❌ | 无 department 表，无同步任务 |
| 访问审计（谁检索到了什么） | ❌ | 只有 `GuardLog`（内容安全）和 `PipelineOperationLog`（解析流水） |
| 数据泄露溯源 | ❌ | 同上 |
| 离职/转岗自动收权 | ❌ | 全靠手工 |

### 2.2 一个已经有的好消息：检索层留了扩展点

`core/nlp/search.py:744+` 的 `retrieval()` 支持 `doc_ids` 白名单过滤；
`api/apps/sdk/session.py:246-263` 已经把 `metadata_condition` 翻译成 `doc_ids` 注入检索：

```python
filtered_doc_ids = meta_filter(metas, convert_conditions(metadata_condition), ...)
if metadata_condition.get("conditions") and not filtered_doc_ids:
    filtered_doc_ids = ["-999"]     # 命中不到就塞一个不存在的 ID，等于返回空
if filtered_doc_ids: req["doc_ids"] = ",".join(filtered_doc_ids)
```

**这套机制原样就能改造成"服务端强制 ACL 过滤"**：把"用户可见 doc_ids"算出来，强制 AND 进
`doc_ids`（而不是像现在这样由请求方可选传入）。这是文档级 ACL 最省力的实现路径——不用动
向量库 schema，不用重建索引。代价是 doc 数量大时 `doc_ids` 列表会很长（ES terms query 有
上限，Milvus 表达式长度也有限制），大概几千个 doc 以内可用。

### 2.3 面向飞书场景的致命矛盾（提前点出来）

飞书机器人接入时，后端只能拿到**一个 APIToken**，而 token 绑的是 tenant 不是人
（`api/db/db_models.py:1213`）。所以默认形态下：

> **全公司所有飞书用户，在 MultiRAG 眼里是同一个身份，共享同一份可见范围。**

这直接把"部门私有数据不外泄"的要求打穿。解法见第二部分 §2.3，但前提是先做完这里的
P0/P1 改造。

---

## 3. 风险定级

| # | 问题 | 严重度 | 触发条件 |
|---|---|:---:|---|
| R1 | `accessible()` 不校验 `permission`，同租户内可越权读私有库 | **高** | 走"单租户多部门"或部门内有私有库 |
| R2 | API Token 无终端用户身份，机器人场景权限全打通 | **高** | 接飞书/任何 bot |
| R3 | 无访问审计，泄露无法溯源 | **高** | 合规审查 / 事后追责 |
| R4 | 无组织架构同步，离职不自动收权 | 中 | 人员流动 |
| R5 | 租户所有权绑个人账号 | 中 | 部门负责人离职 |
| R6 | 无跨部门共享机制 | 中 | 公共制度类文档 |
| R7 | 模型配置按租户重复 N 份 | 低（运维痛） | 部门数上去之后 |
| R8 | `configs/service_conf.yaml:215-237` 的 `oauth` 段没有 `feishu` 键，但 `common/settings.py:209` 和 `api/apps/user_app.py:289-335` 引用了 `settings.FEISHU_OAUTH` | 低 | 一旦有人访问 `/feishu_callback` 就 `TypeError` |

---

## 4. 改造路线图

### P0 — 止血（必做，估 1～2 周）

| 项 | 改动 | 难度 |
|---|---|---|
| 修 `accessible()` | 在 `KnowledgebaseService.accessible` / `DocumentService.accessible` 里补 `permission == team or tenant_id == user_id` 判断；或直接改成调用 `check_kb_team_permission` | 低 |
| 统一权限入口 | 把散在 12+ 个文件里的三套检查（`accessible` / `check_kb_team_permission` / 手工遍历 tenants）收敛成一个 `authz.can_read_kb(user, kb)` | 中（改动面广，但机械） |
| 补权限回归测试 | `tests/unit/` 下加一组"normal 成员 vs me 库"的用例，防回归 | 低 |
| 补 `feishu` oauth 配置段 | `configs/service_conf.yaml` 加 key，或给 `FEISHU_OAUTH` 加 None 保护 | 低 |

**风险**：修 `accessible()` 会让一些"之前能访问"的调用变成 403。上线前要跑一遍现有用户的
KB `permission` 分布，必要时先批量把存量库刷成 `team`。

### P1 — 组织化（估 4～6 周）

引入三张表，用 Alembic 迁移（`configs/alembic/versions/` 已有 33 个版本，流程成熟）：

```
Department      : id, tenant_id, parent_id, name, external_id(飞书 open_department_id), path
UserDepartment  : user_id, department_id, is_leader
ResourceGrant   : subject_type(user|department|role), subject_id,
                  resource_type(kb|agent|doc), resource_id,
                  actions(read|write|manage), granted_by, expire_at
```

- `Knowledgebase.permission` 保留但降级为"默认值"，真正判定走 `ResourceGrant`
- 查询侧：`get_by_tenant_ids` 的 WHERE 从 `permission == team` 换成 `EXISTS(ResourceGrant ...)`
- **兼容策略**：迁移时把每个 `permission=team` 的库自动生成一条 `(department, tenant的默认部门, read)` 授权，老数据零感知

难度：中。主要成本在"把所有列表查询的 WHERE 改掉"，涉及 `knowledgebase_service.py`、
`canvas_service.py`、`document_service.py`、`dialog_service.py` 及各自的 restful 层。

同期做：
- **审计表** `AccessAuditLog(user_id, tenant_id, action, resource_type, resource_id, kb_ids, doc_ids, query_text_hash, ip, ua, ts)`，在检索/下载/预览三个动作埋点。建议异步写（复用现有 Redis Stream）
- **APIToken 加 `end_user_id` 透传**：请求头带 `X-End-User-Id`，服务端解析成内部 user 后计算可见范围。这是 R2 的解，也是飞书方案的前提

### P2 — 文档级 ACL（估 3～4 周，依赖 P1）

用 §2.2 的 `doc_ids` 强制过滤机制实现：

1. 在检索入口（`chunk_app.retrieval_test`、`dialog_service.chat`、`sdk/session.py`）统一插一层
   `resolve_visible_doc_ids(user, kb_ids)`
2. 结果与请求方传入的 `doc_ids` 取**交集**（不是并集）
3. 空集时注入 `["-999"]` 走现成的"返回空"路径

难度：中低。核心逻辑就一个函数，难点是要保证**所有检索入口都覆盖到**——建议加一个
import-linter 契约或单测，禁止绕过 `retrieval()` 的封装直接调 `settings.retriever`。

### P3 — chunk 级 ACL（估 6～10 周，最贵）

同一个库内分密级（比如 HR 库里普通制度全员可见、薪酬细则仅 HRBP 可见）。

必须动的地方：
- `configs/es_mapping.json` + `configs/infinity_mapping.json` + Milvus schema：加 `acl_kwd`（数组字段）
- `core/svr/task_executor.py`：写入 chunk 时带上 ACL 标签
- `core/nlp/search.py`：所有 filter 构造处强制 AND 一个 `acl_kwd IN (用户的主体集合)`
- **存量数据要重建索引**（这是最大的成本，取决于文档量，可能是几小时到几天）

难度：高。建议只在确有"同库分密级"需求时才做，否则用 P2 的"一个密级一个库"绕过去。

### P4 — 与飞书通讯录打通（估 2～3 周，依赖 P1）

见第二部分 §3.4。

### 路线总览

```
P0 止血 ──┬── 1-2周 ── 必做，不做就不能上线
          │
P1 组织化 ─┼── 4-6周 ── Department/Grant/审计/end_user_id
          │
P2 文档ACL ┼── 3-4周 ── 依赖 P1
          │
P4 飞书同步 ┴── 2-3周 ── 依赖 P1，可与 P2 并行
          │
P3 chunk ACL ─── 6-10周 ── 按需，可以永远不做
```

**如果只想尽快上线**：做完 P0，按"部门=租户"（方案 A）铺开，接受 §2 列的 5 个坑。
**如果要撑 1 年以上**：P0 + P1 是最低配。

---

# 第二部分：飞书接入方案

## 1. 现状盘点

代码里**已有的**：
- 飞书 OAuth 登录（`api/apps/user_app.py:289-398`，`user_info_from_feishu` 在 `:783-788`），
  走 `authen/v1/user_info`，用 email 匹配/创建用户，`login_channel="feishu"`
- 配置位 `common/settings.py:209` → `settings.FEISHU_OAUTH`

代码里**没有的**：
- 飞书机器人（消息事件接收/回复）——零
- 飞书文档/知识库连接器——`common/data_source/config.py:43-73` 的 `DocumentSource` 枚举里
  有 `DINGTALK_AI_TABLE`，**没有 feishu/lark**
- `configs/service_conf.yaml:215-237` 的 `oauth` 段实际没有 `feishu` 键（见 R8）

所以飞书这块基本是从零开始，但有一个可复用的登录通道。

## 2. 三种接入形态

### 形态 1：每个部门一个飞书应用 —— 最省事，不推荐长期用

飞书自建应用有两个**互相独立**的配置：
- **可用范围**（谁能用这个应用）
- **通讯录范围**（这个应用能读到谁的信息）

给每个部门建一个自建应用，可用范围限定到该部门，后端各配一个 APIToken 指向该部门的租户。

- 优点：**零后端改造**，当天就能跑通；隔离靠飞书 + 租户双保险
- 缺点：N 个部门 = N 个应用 = N 套 app_id/secret/事件订阅/审核发版；用户在飞书里看到 N 个机器人；
  跨部门问答做不了

> 适合：PoC 阶段，或部门数 < 5 且长期不会变。

### 形态 2：一个应用 + 后端按人授权 —— **推荐**

一个飞书机器人，所有人用同一个。后端根据消息发送者的 `open_id` 解析出内部用户，
再计算这个人能看哪些库。

前置条件（就是第一部分的 P0/P1）：
- APIToken 支持 `end_user_id` 透传
- 有 Department + ResourceGrant，能算出"某人可见的 kb_ids"

流程：
```
飞书用户 @机器人
  → im.message.receive_v1 事件（含 sender.sender_id.open_id）
  → 网关：open_id → (缓存/通讯录 API) → email → MultiRAG user_id
  → 调 MultiRAG，带 X-End-User-Id: <user_id>
  → 服务端计算 visible_kb_ids ∩ dialog.kb_ids，只在交集里检索
  → 流式卡片回写飞书
```

- 优点：一个机器人、一套配置；权限跟着人走；转岗离职自动生效（配合 P4 同步）
- 缺点：依赖后端改造完成

### 形态 3：权限镜像 —— 最理想，最贵

把飞书云文档/知识库的协作者权限，同步成 MultiRAG 里的 chunk ACL：谁在飞书能打开这篇文档，
谁在机器人里就能检索到它。

- 需要 P3（chunk 级 ACL）+ 云文档权限 API 轮询/事件
- 云文档权限变更没有可靠的全量事件推送，实际要定期全量对账，成本高
- **建议：先不做**。用"知识库 → MultiRAG KB → 部门授权"的粗粒度映射就够了

---

## 3. 详细技术方案（按形态 2）

### 3.1 架构

```
┌──────────────┐   WebSocket 长连接 或 Webhook
│  飞书开放平台 │ ─────────────────────────────┐
└──────────────┘                              ▼
                                    ┌──────────────────────┐
                                    │  feishu-gateway      │  ← 新增独立服务
                                    │  · 事件收敛/去重     │
                                    │  · open_id ↔ user 映射│
                                    │  · 会话上下文管理    │
                                    │  · 流式卡片回写      │
                                    └──────────┬───────────┘
                                               │ HTTP + X-End-User-Id
                                               ▼
                                    ┌──────────────────────┐
                                    │  MultiRAG API :8123  │
                                    │  /api/v1/chats_openai│
                                    │  /api/v1/agents_...  │
                                    └──────────────────────┘
```

**建议做成独立服务而不是塞进 `api/apps/`**，理由：
- 长连接模式是有状态的，跟 FastAPI 多副本模型冲突（见 §3.5 坑 1）
- 飞书侧的重试/限流/卡片状态机跟 RAG 主流程关注点完全不同
- 独立服务好单独扩缩容和灰度

如果坚持塞进主服务，放 `api/apps/feishu_app.py` + `api/apps/services/feishu_service.py`，
只用 Webhook 模式。

### 3.2 飞书侧接口清单

#### 接收消息

| 项 | 内容 |
|---|---|
| 事件 | `im.message.receive_v1` |
| 权限点（任选） | `im:message.p2p_msg:readonly`（私聊）/ `im:message.group_at_msg:readonly`（群里@）/ `im:message.group_msg`（群全量） |
| 关键字段 | `event.sender.sender_id.{open_id,union_id,user_id}`、`event.message.{message_id, chat_id, chat_type(p2p\|group), message_type, content, root_id, parent_id, thread_id, mentions}` |
| 注意 | 想拿到 `sender_id.user_id` 需额外申请 `contact:user.employee_id:readonly` |

两种接入模式：

**A. 长连接（WebSocket）** —— 官方 SDK 内建，服务端只需能出公网，不需要公网 IP/域名，
免解密免验签。开发周期从约 1 周降到 5 分钟。
**⚠️ 但：长连接是集群模式且不广播——同一应用部署多副本时，只有随机一个副本收到消息。**
生产要么单副本，要么用 B。

**B. Webhook** —— 配置回调地址，需要公网可达 + 验签 + 解密。适合多副本。

> 建议：**开发/PoC 用 A，生产用 B**；或生产用 A 但 gateway 固定单副本 + 主备切换。

#### 回复消息

| 用途 | 接口 |
|---|---|
| 回复某条消息 | `POST /open-apis/im/v1/messages/:message_id/reply` |
| 主动发消息 | `POST /open-apis/im/v1/messages` |
| 权限点 | `im:message:send_as_bot` / `im:message` |

#### 流式卡片（打字机效果）—— AI 场景的标配

这是飞书 2025 年为 AI 场景推出的 **CardKit** 能力。

| 步骤 | 接口 | 限流 |
|---|---|---|
| 1. 创建卡片实体 | `POST /open-apis/cardkit/v1/cards`（body: `type=card_json`, `data`），返回 `card_id` | 1000 次/分、50 次/秒 |
| 2. 发消息带上 card_id | `POST /open-apis/im/v1/messages` | — |
| 3. 流式追加文本 | `PUT /open-apis/cardkit/v1/card-element/content` | 单卡片实体 10 次/秒 |
| 4. 改卡片配置 | `PATCH /open-apis/cardkit/v1/card/settings` | 同上 |
| 5. 追加组件（如引用来源、按钮） | `POST /open-apis/cardkit/v1/card-element` | 同上 |
| 权限点 | `cardkit:card:write` | |

**约束**：
- 必须用**卡片 JSON 2.0** 结构
- 飞书客户端 **≥ 7.20** 才支持；流式参数自定义要 **≥ 7.23**（低版本会降级成整块刷新，不会报错但体验差）
- 单卡片 10 次/秒 → LLM token 流出来要**做节流合并**，建议 100～150ms 一批，别一个 token 调一次
- 官方说明：在流式更新模式下持续调用不触发 QPS 限制，但仍建议自己做节流兜底

#### 卡片交互（按钮：换个模型重答、看引用、反馈👍👎）

| 项 | 内容 |
|---|---|
| 回调类型 | `card.action.trigger`（固定值） |
| 回调字段 | `tenant_key`、`app_id`、`user_id`、`open_id`、`token`、`action.value` |
| 注意 | 回调里的 `token` **仅 30 分钟有效，最多更新卡片 2 次** |

#### 身份映射（open_id → 内部用户）

| 接口 | `GET /open-apis/contact/v3/users/:user_id` |
|---|---|
| 参数 | `user_id_type ∈ {open_id, union_id, user_id}` |
| 返回 | `open_id`、`union_id`、`user_id`、`email`、`enterprise_email`、`employee_no`、`department_ids` |
| 权限点 | `contact:user.base:readonly` 等（部分敏感字段需单独申请；部门路径需 `user_access_token`） |

映射策略建议：
1. 建 `t_ai_feishu_identities(open_id, union_id, feishu_user_id, multirag_user_id, department_ids, synced_at)` 表
2. **首选 `union_id` 做主键**——`open_id` 是应用维度的，换个应用就变；`union_id` 跨应用稳定
3. 邮箱只在首次绑定时用（对上 `t_ai_users.email`），之后走 ID 映射，避免改邮箱就失联
4. 加 Redis 缓存，TTL 10 分钟

#### 部门 / 组织架构同步

| 用途 | 接口 |
|---|---|
| 部门详情/子部门 | `contact/v3/departments`（`department_id_type=open_department_id`） |
| 部门直属用户 | `contact/v3/users?department_id=...` |
| 变更事件 | `contact.user.created/updated/deleted_v3`、`contact.department.created/updated/deleted_v3` |

同步策略：**事件增量 + 每日全量对账**。只靠事件会漏（有丢事件的可能），只靠全量太慢。

#### 知识库内容同步（可选，Phase 3）

| 用途 | 接口 | 限流 |
|---|---|---|
| 知识空间列表 | `GET /open-apis/wiki/v2/spaces` | |
| 子节点列表 | `GET /open-apis/wiki/v2/spaces/:space_id/nodes` | |
| 节点详情（拿 `obj_token`/`obj_type`） | `GET /open-apis/wiki/v2/spaces/get_node` | |
| 文档纯文本 | `GET /open-apis/docx/v1/documents/:document_id/raw_content` | **5 次/秒** ⚠️ |
| 权限点 | `docx:document:readonly` / `wiki:wiki:readonly` | |

**⚠️ `raw_content` 只有 5 次/秒**，超限返回 HTTP 400 + 错误码 `99991400`。同步一个 5000 篇文档的
知识空间至少要 ~17 分钟（还没算节点遍历）。必须做指数退避 + 断点续传 + 增量（按
`node.obj_edit_time` 判断是否需要重拉）。

落地建议：在 `common/data_source/` 下新增 `feishu_wiki_connector.py`，
`DocumentSource` 枚举加 `FEISHU_WIKI = "feishu_wiki"`。现有连接器框架（Notion/Confluence 等 25 个）
是现成的模板，改造量不大。

#### 鉴权与限流通则

- `tenant_access_token` 有效期**最长 2 小时**，必须缓存 + 提前刷新（建议提前 10 分钟）。
  官方 SDK 自带刷新，自己写 HTTP 就要自己管
- 自定义机器人（webhook 机器人）限流 100 次/分、5 次/秒——**注意这跟自建应用是两回事**，
  我们用的是自建应用，限流按各接口的策略等级走
- 超限返回 HTTP **429**，响应头带建议等待时间；部分接口（如 `raw_content`）返回 400 + `99991400`

### 3.3 MultiRAG 侧对接点

用现成的 OpenAI 兼容端点，最省事：

```
POST /api/v1/chats_openai/{chat_id}/chat/completions      # 对话应用（Dialog）
POST /api/v1/agents_openai/{agent_id}/chat/completions    # Agent 工作流
```
（`api/apps/sdk/session.py:280, 568`，鉴权 `Authorization: Bearer <APIToken.token>`）

需要新增的能力（P1 的一部分）：
1. 认识 `X-End-User-Id` 头，解析成内部 user_id
2. 在 `chat_completion` 里把 `dialog.kb_ids` 与该用户可见 kb_ids 取交集
3. 会话隔离：`API4Conversation.user_id`（`api/db/db_models.py:1229`）已经有了，
   gateway 传飞书 `chat_id + thread_id` 的哈希进去即可

### 3.4 分期计划

| Phase | 内容 | 估时 | 依赖 |
|---|---|---|---|
| **P0 PoC** | 单部门单应用（形态 1），长连接，纯文本回复，跑通链路 | 1～2 周 | 无 |
| **P1 生产可用** | Webhook 模式、流式卡片、消息去重、会话上下文、错误兜底、引用来源展示 | 3～4 周 | P0 |
| **P2 权限对齐** | 切形态 2：身份映射表、`X-End-User-Id`、可见范围计算 | 2～3 周 | 后端 P0+P1 |
| **P3 组织同步** | 通讯录事件 + 每日对账，自动建/停用用户、维护部门归属 | 2～3 周 | 后端 P1 |
| **P4 内容同步** | `feishu_wiki_connector`，飞书知识空间 → MultiRAG KB | 2～3 周 | 无强依赖 |

总计约 10～15 周（单人）。P4 可以跟 P2/P3 并行。

### 3.5 已知的坑（按踩到的概率排序）

1. **长连接多副本只有一个收到消息**。飞书官方明确说明长连接是集群模式、不广播。
   K8s 里 replicas=3 会让 2/3 的实例空转，且哪个收到是随机的 → 生产用 Webhook，
   或 gateway 单副本 + Leader 选举。

2. **事件会重推，必须幂等**。飞书在没收到 200 时会重试。用 `message_id` 做 Redis
   `SETNX`（TTL 1 小时）去重。**这个不做，用户会看到机器人重复回答**。

3. **必须先回 200 再异步处理**。飞书对事件回调有响应时间要求，RAG 一轮下来动辄 5～30 秒。
   正确姿势：收到事件立即返回 200 → 起异步任务 → 先发一张"思考中"的卡片 → 流式更新它。

4. **卡片刷新频率**。10 次/秒是硬约束。LLM 首 token 快时很容易超。做 100～150ms 的
   时间窗合并，并且**按字符增量而非全量重发**。

5. **客户端版本参差**。公司里总有人不更新飞书。JSON 2.0 要 ≥7.20。要么在卡片里做降级
   （检测失败就退回普通文本消息），要么先统计一下公司客户端版本分布。

6. **群聊 vs 私聊语义不同**。群里要 @机器人才触发（`im:message.group_at_msg:readonly`），
   且 `mentions` 里要剔掉 @机器人本身的部分再送给 LLM，否则问题里会带一串 `@_user_1`。

7. **富文本/图片/文件消息**。`message_type` 有 `text`/`post`/`image`/`file`/`audio` 等。
   最少要处理 `text` 和 `post`（富文本，content 是嵌套 JSON）。图片要走
   `im/v1/messages/:message_id/resources/:file_key` 下载再送多模态模型。

8. **⚠️ 出网问题**。gateway 需要出站访问 `open.feishu.cn`（HTTPS）和长连接的 WSS。
   按之前记录，公司网络会拦截直连 TLS——**这条要在 PoC 之前先确认清楚**，否则会浪费很多
   时间去 debug 一个不是代码的问题。需要提前申请白名单或走公司出口代理。

9. **审核发版流程**。自建应用改权限点要走企业管理员审核。**每加一个 scope 就要审一次**，
   建议一次性把 §3.2 里要用的权限点全列出来一起申请，别挤牙膏。

10. **`user_access_token` vs `tenant_access_token`**。部门路径等敏感字段需要用户级 token。
    如果要做形态 3 的权限镜像，绕不开引导用户 OAuth 授权——现有的
    `api/apps/user_app.py:289` 飞书登录可以复用，但它用的是 `authen/v1`，建议升级到
    v2 OAuth（`/open-apis/authen/v2/oauth/token`）。

---

## 4. 两部分的合并建议

飞书接入的价值上限，由后端多租户的改造进度决定：

| 后端状态 | 飞书能做到什么 |
|---|---|
| 现状（不改） | 只能形态 1：每部门一个应用。或者形态 2 但**所有人共享同一份可见范围**（不可接受） |
| 做完 P0 | 同上，但至少 Web 端不会越权 |
| 做完 P0+P1 | 形态 2 可用：一个机器人、权限跟人走 ← **推荐的目标态** |
| 做完 P0+P1+P2 | 文档级授权，跨部门定向共享单篇文档 |
| 做完 P3 | 同库分密级，可做形态 3 的权限镜像 |

**建议的执行顺序**：

```
第 1-2 周   后端 P0（止血）        ∥  飞书 P0（PoC，形态 1，验证出网+链路）
第 3-8 周   后端 P1（组织化）      ∥  飞书 P1（流式卡片、生产化）
第 9-11 周  后端 P2（文档 ACL）    ∥  飞书 P2（切形态 2）
第 12-14 周 飞书 P3（组织同步）+ P4（内容同步）
```

后端和飞书两条线可以并行，交汇点在第 9 周（飞书 P2 依赖后端 P1 产出的
`X-End-User-Id` + 可见范围计算）。

---

## 附：引用来源

飞书官方文档：
- [流式更新卡片 · 开发指南](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview?lang=zh-CN)
- [创建卡片实体](https://open.feishu.cn/document/cardkit-v1/card/create)
- [更新卡片实体配置](https://open.feishu.cn/document/cardkit-v1/card/settings?lang=zh-CN)
- [飞书卡片资源概述](https://open.feishu.cn/document/cardkit-v1/feishu-card-resource-overview?lang=zh-CN)
- [接收消息事件 im.message.receive_v1](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive?lang=zh-CN)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)
- [使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)
- [Python SDK 处理事件](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events?lang=zh-CN)
- [获取单个用户信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get?lang=zh-CN)
- [部门资源介绍](https://open.feishu.cn/document/server-docs/contact-v3/department/field-overview?lang=zh-CN)
- [通讯录概述](https://open.feishu.cn/document/server-docs/contact-v3/resources?lang=zh-CN)
- [获取文档纯文本内容](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/raw_content)
- [获取知识空间节点信息](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node?lang=zh-CN)
- [自建应用获取 tenant_access_token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal?lang=zh-CN)
- [配置卡片交互](https://open.feishu.cn/document/common-capabilities/message-card/add-card-interaction/interaction-module?lang=zh-CN)
- [如何为应用开通云文档相关资源的权限](https://open.feishu.cn/document/faq/trouble-shooting/how-to-add-permissions-to-app?lang=zh-CN)
- [频控策略](https://feishu.apifox.cn/doc-1939846)
- [管理员查看和配置已安装应用（可用范围/通讯录范围）](https://www.feishu.cn/hc/zh-CN/articles/157207073325)
