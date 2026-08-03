# RAGFlow 上游对照：对前三份报告的修订与补充

> 生成日期：2026-07-28
> 上游基线：`infiniflow/ragflow@main`（86,242★，Apache-2.0，最后推送 2026-07-28）
> 前置：[多租户评估](multitenancy-and-feishu-report.md) · [生态调研](feishu-ecosystem-analysis.md) · [自建方案](feishu-inhouse-plan.md)
> 本文是本地评审笔记（`internal/*.md`，不入库）

---

## 1. ⚠️ 最重要的修订：那个越权读，上游三个月前就修了

我在第一份报告里把 `accessible()` 不校验 `permission` 定性为"同一语义两套实现不一致"。
**这个判断不完整。准确的事实是：**

> **这是 RAGFlow 上游的老 bug，上游已于 2026-05-09 修复
> （[PR #14645](https://github.com/infiniflow/ragflow/pull/14645)，merge commit `3b6eeabb`），
> 我们只是没跟这个提交。**

上游 PR 标题原文就叫 **"Fix: private dataset authorization bypass in shared dataset access checks"**，
描述的问题与我们的完全一致：标记为 `permission = me` 的私有数据集，可以被同租户其他成员
通过 `KnowledgebaseService.accessible()` / `DocumentService.accessible()` 访问。

对比一下就一目了然 —— 上游**修复前**的实现：

```python
# ragflow, pre-#14645
docs = cls.model.select(cls.model.id).join(
    UserTenant, on=(UserTenant.tenant_id == Knowledgebase.tenant_id)
).where(cls.model.id == kb_id, UserTenant.user_id == user_id).paginate(0, 1)
return bool(docs.dicts())
```

我们现在的实现（`api/db/services/knowledgebase_service.py:512-525`）：

```python
tenant_id = ...                                   # 取 KB 的 tenant_id
membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
if not membership: return False
return UserTenantService.can_access_tenant_resources(membership.role)
```

语义完全一样 —— "是这个租户的成员就放行"。**我们是忠实地把上游的旧版本翻成了 SQLAlchemy，
不是自己写错了。** 这个定性变化很重要，因为它决定了修法（见 §2）。

### 1.1 受影响的函数比我上一版说的多 2 个

PR #14645 改了 **4 个**函数，我上一版只点出了 2 个：

| 函数 | 我上版是否点出 | 我们的位置 |
|---|:---:|---|
| `KnowledgebaseService.accessible` | ✅ | `knowledgebase_service.py:512-525` |
| `DocumentService.accessible` | ✅ | `document_service.py:2542-2560` |
| **`KnowledgebaseService.get_kb_by_id`** | ❌ **漏了** | `knowledgebase_service.py:527-531` |
| **`KnowledgebaseService.get_kb_by_name`** | ❌ **漏了** | `knowledgebase_service.py:533-537` |

后两个我们的实现是：

```python
query = db.query(cls.model).join(UserTenant, UserTenant.tenant_id == cls.model.tenant_id) \
          .filter(cls.model.id == kb_id, UserTenant.user_id == user_id).limit(1)
```

——**跟上游修复前的那段 join 一模一样**，同样的越权。

### 1.2 上游的修法（照抄即可）

```python
# KnowledgebaseService.accessible —— 上游修复后
e, kb = cls.get_by_id(kb_id)
if not e: return False
if kb.status != StatusEnum.VALID.value: return False
if kb.tenant_id == user_id: return True                       # 本人即租户主
if kb.permission != TenantPermission.TEAM.value: return False  # ← 关键的一行
joined_tenants = TenantService.get_joined_tenants_by_user_id(user_id)
return any(t["tenant_id"] == kb.tenant_id for t in joined_tenants)

# DocumentService.accessible —— 改成纯委派，不再自己 join
e, doc = cls.get_by_id(doc_id)
if not e: return False
return KnowledgebaseService.accessible(doc.kb_id, user_id)

# get_kb_by_id —— 走 accessible
e, kb = cls.get_by_id(kb_id)
if not e or not cls.accessible(kb_id, user_id): return []
return [kb.to_dict()]

# get_kb_by_name —— 遍历同名 KB，返回第一个有权限的
kbs = cls.query(name=kb_name, status=StatusEnum.VALID.value)
for kb in kbs:
    if cls.accessible(kb.id, user_id): return [kb.to_dict()]
return []
```

PR 统计：3 个文件、+146/−25，其中 **119 行是新增的回归测试**
（`test/unit_test/api/db/services/test_dataset_access_permissions.py`）——那份测试也一并移植。

**注意上游 `DocumentService.accessible` 改成了纯委派**。我们目前是把租户成员判断
**复制**了一份到 `document_service.py`。这种"复制而非委派"的分叉，是**未来每次移植
这两个文件都要手工解冲突**的根源。改成委派既修了 bug，也消掉了长期的移植成本。

---

## 2. 对 P0 任务的修订：不要自己发明，照上游改

我之前建议"新增 `api/common/authz.py` 统一入口"。**在"要长期跟进上游"的前提下，这个建议要收回。**

| | 自己发明统一入口 | 照上游语义改 ⭐ |
|---|---|---|
| 修 bug | ✅ | ✅ |
| 未来移植这两个文件 | ❌ 每次都冲突，且要人工判断语义是否等价 | ✅ 语义对齐，diff 干净 |
| 回归测试 | 自己写 | ✅ 直接移植上游的 119 行 |
| 工作量 | 中 | **小** |

**新的 P0 定义**：用 `port-ragflow-commit` skill 跟进这三个提交，翻成我们的 SQLAlchemy 写法：

| 提交 | 日期 | 内容 | 优先级 |
|---|---|---|---|
| [`3b6eeabb` / #14645](https://github.com/infiniflow/ragflow/pull/14645) | 2026-05-09 | **私有数据集越权读修复**（4 个函数 + 回归测试） | **P0，必做** |
| [`592dba14` / #13627](https://github.com/infiniflow/ragflow/pull/13627) | 2026-05-11 | 新增私有 helper `_visibility_and_status_filter` | P0.5，强烈建议 |
| `53afc323` / #17370 | 2026-07-27 | Fix get datasets owner retrieve the whole dataset（引入 `get_owner_filter`） | P1，需确认是否影响我们 |

### 2.1 `_visibility_and_status_filter` 为什么值得一起跟

上游把这段可见性过滤表达式收成了一个私有 helper：

```python
@classmethod
def _visibility_and_status_filter(cls, joined_tenant_ids, user_id):
    return ((cls.model.tenant_id.in_(joined_tenant_ids)
             & (cls.model.permission == TenantPermission.TEAM.value))
            | (cls.model.tenant_id == user_id)) & (cls.model.status == StatusEnum.VALID.value)
```

被 4 个方法复用：`get_by_tenant_ids`、`get_all_kb_by_tenant_ids`、`get_list`、`get_owner_filter`。

**我们现在是把这段表达式原样复制粘贴在 4 个地方**——
`knowledgebase_service.py:107`、`:172`、`:223`、`:485`。

跟进这个 helper 有两个好处：
1. 以后上游改可见性规则（很可能会，比如加 department），只改一处，我们移植也只改一处
2. **这正好是 P1 的落点**——我们要加的部门/授权判断，改这一个 helper 就能覆盖 4 个查询路径

---

## 3. 部门 / RBAC：上游明确不会给，我们的 P1 是永久分叉

我查了上游所有相关 issue：

| # | 标题 | 状态 | 时间 |
|---|---|---|---|
| 6996 | **Missing Department and Group Functionality** | closed | 2025-04-14 |
| 3684 | RBAC implements in user APIs | closed | 2024-11-27 |
| 2588 | User Management and RBAC（20+ 个 👍） | closed | 2024-09-25 |
| 6076 | User hierarchy or RBAC support | closed | 2024-03-14 |

**四条全部 closed，而 `UserTenantRole` / `TenantPermission` 两个枚举至今没变**
（我核对了上游 `api/db/__init__.py` 当前内容，仍是 `owner/admin/normal/invite` 和 `me/team`）。

结论：**部门、用户组、资源级授权，上游不打算做。我们的 P1 是一次永久性的分叉。**

### 3.1 那就要按"永久分叉"来设计，把移植成本压到最低

三条设计原则：

1. **新增，不改造**。`Department` / `UserDepartment` / `ResourceGrant` 三张表放新文件
   （建议 `api/db/services/authz_service.py`），**不动 `knowledgebase_service.py` 的既有方法签名**。

2. **所有增强只从 `_visibility_and_status_filter` 一个口子进**。
   把它改成：
   ```python
   base = <上游原样的表达式>
   if FEATURE_DEPARTMENT_ACL:
       base = base | <部门授权表达式>
   return base
   ```
   这样上游改这个 helper 时，我们的 diff 永远只有那两行加号。

3. **`accessible()` 保持上游语义，增强走装饰/包装**。
   即：`accessible()` 一字不改地跟上游，另起 `accessible_with_grants()` 在外层叠加部门判断。
   调用点改指向新函数。上游改 `accessible()` 时零冲突。

这三条做到了，P1 那 4~6 周的工作对未来的移植成本影响能压到接近零。

---

## 4. 飞书：上游没有，也不会有，但有一个可借的东西

我核对了上游 `common/data_source/` 目录，**没有任何 feishu / lark 连接器**，只有
`dingtalk_ai_table_connector.py`（跟我们一样）。**飞书这块完全是我们自己的活儿**——
好消息是没有移植冲突风险，坏消息是没有免费午餐。[自建方案](feishu-inhouse-plan.md)不受影响。

**但上游有一个我们没有的东西值得注意**：`rest_api_connector.py`（通用 REST 连接器）。

如果它做得够通用，**飞书知识空间的内容同步（我上一版说的 P4）可能不用写专门的连接器**，
配一个 REST 连接器 + 认证头就能拉 `wiki/v2/spaces/:id/nodes` 和
`docx/v1/documents/:id/raw_content`。**值得先移植它再评估**，可能省掉 2~3 周。

⚠️ 注意 `raw_content` 只有 5 次/秒的限流，通用 REST 连接器不见得有退避策略，要验。

---

## 5. 其他值得跟进的上游变更

### 5.1 我们缺的连接器（6 个）

上游 `common/data_source/` 有、我们目录里没有的文件：

```
azure_blob_connector.py      bigquery_connector.py      onedrive_connector.py
outlook_connector.py         rest_api_connector.py ⭐   salesforce_connector.py
```

（`azure_blob` 请先确认我们是不是已经用 `blob_connector.py` 覆盖了，CLAUDE.md 里提到过 Azure Blob。）

对公司场景，优先级：**`rest_api_connector` > `onedrive` / `outlook`（如果同时用 M365）> 其余**。

### 5.2 MCP server 已经分叉

| | 上游 | 我们 |
|---|---|---|
| 工具名 | `ragflow_list_datasets`、`ragflow_list_chats` | `list_datasets`、`multirag_retrieval` |
| 资源 | — | `datasets://list`（我们独有） |
| 中间件 | — | ErrorHandling / RateLimiting / Timing / StructuredLogging（我们独有） |

我们的 `mcp/server/server.py` 已经比上游走得远（有速率限制、结构化日志，还刻意注掉了
`ResponseCachingMiddleware` 因为它的 cache key 不含用户身份、多租户下会跨租户泄漏检索结果
——这个判断很对，别被上游后续版本改回去）。

上游 v0.26.4 修了一个 MCP bug（[#16639](https://github.com/infiniflow/ragflow/pull/16639)）：
`list_chats` 期望列表但 `/chats` API 返回的是分页字典。**我们如果以后加 `list_chats` 工具，
注意别踩同一个坑。**

### 5.3 上游近期与权限相关的其他动作

- `dc4b8252` / #14595（2026-05-29）**Feat: tenant llm provider** —— 租户级 LLM provider，
  跟我们已有的 `TenantLLM` + `tenant_llm_id` 相关，确认是否已跟进
- `62cb2926` / #13072（2026-03-05）Feat/tenant model —— 我们已有 `tenant_llm_id` 等字段，应该跟过了
- v0.26.4 release note 提到 "add document and file access checks"、"get team member's chat"、
  "get team's search in own search-list" —— 都是权限面的小修补，**跟 P0 一批过一遍**

---

## 6. 对前三份报告的具体修订

| 报告 | 原结论 | 修订 |
|---|---|---|
| 多租户评估 §2/§3 R1 | "同一语义两套实现不一致，属于要修的 bug" | ✅ 结论不变，但**定性改为"上游已修、我们没跟"**；受影响函数从 2 个增加到 **4 个** |
| 多租户评估 §4 P0 | "新增 `api/common/authz.py` 统一入口，收敛 12+ 文件的调用" | ❌ **收回**。改为"照 #14645 语义原地修 4 个函数 + 移植 #13627 的 helper + 移植上游 119 行回归测试"。工作量更小、未来移植更干净 |
| 多租户评估 §4 P1 | 三张表 + 改所有列表查询的 WHERE | ⚠️ 补充**三条防分叉设计原则**（§3.1），把移植成本压到接近零 |
| 生态调研 | AstrBot vs LangBot | 不变（上游无相关内容） |
| 自建方案 | lark-oapi + 抄 LangBot | ⚠️ 补充：**先移植 `rest_api_connector.py` 再评估飞书知识空间同步**，可能省 2~3 周 |

---

## 7. 修订后的动作清单（按优先级）

```
本周
  □ 验出网（open.feishu.cn HTTPS + WSS）—— 不通的话飞书线全部作废
  □ 统计公司飞书客户端版本分布（<7.20 占比）
  □ 用 port-ragflow-commit 跟进 #14645（4 个函数 + 119 行回归测试）  ← 安全洞，最高优先级
  □ 跟进 #13627（_visibility_and_status_filter）

第 1-2 周
  □ 核对 #17370、v0.26.4 的权限面小修补
  □ 评估移植 rest_api_connector.py
  □ lark-oapi 跑通飞书最小闭环

第 3-8 周
  □ P1 组织化（严格遵守 §3.1 的三条防分叉原则）
  □ 飞书流式卡片 + 消息类型（抄 LangBot）
```

---

## 附：本文引用

- [infiniflow/ragflow](https://github.com/infiniflow/ragflow)
- [PR #14645 Fix: private dataset authorization bypass in shared dataset access checks](https://github.com/infiniflow/ragflow/pull/14645)（merge `3b6eeabb`，2026-05-09）
- [PR #13627 Refact: Added a private helper `_visibility_and_status_filter`](https://github.com/infiniflow/ragflow/pull/13627)（`592dba14`，2026-05-11）
- PR #17370 Fix get datasets owner retrieve the whole dataset（`53afc323`，2026-07-27）
- [PR #16639 MCP list_chats 分页字典修复](https://github.com/infiniflow/ragflow/pull/16639)
- [PR #14595 Feat: tenant llm provider](https://github.com/infiniflow/ragflow/pull/14595)
- 上游 RBAC/部门相关 issue：#6996、#3684、#2588、#6076（**全部 closed，未实现**）
