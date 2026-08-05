# Channel 子系统加固与通用化 · 项目文档集

> 项目代号：**CHN**（Channel Program）
> 建立日期：2026-08-05 · 后端审计基线：`main` @ `75f125d5` · 前端基线：`web` @ `2c32928`
> 上游对照基线：`infiniflow/ragflow` @ `d6f1475c5c1fe266a6eab2c0acee9722d6720fea`

## 五分钟上手

1. 读本文件，重点是 §3（现在该干什么）、§5（跨仓部署顺序）、§6（硬不变量）。
2. 读 [PROGRESS](PROGRESS.md) 顶部的「维护协议（MANDATORY）」——它约束你怎么记账。
3. 只读你要动的那个阶段的任务表，其余略过。
4. 动到前后端接口就读 [CONTRACT](CONTRACT.md)；动到已上线行为就读
   [`api/channels/README.md`](../../api/channels/README.md) 的「已实现 / 尚未实现或不能宣称」。
5. 需要背景（为什么这么判、怎么复现）→ `internal/channel-audit-2026-08.md`（本地笔记，不入库）。
   不在你的工作树里就跳过——任务行里的 `file:line` 锚点足够动手。

## 1. 这是什么

对已上线的 Channel 子系统（飞书 managed channel + supervisor + worker + 前端 `settings/channels` 页）
做的一次跨三仓深度审计的收敛结论与执行账本。四条主线：

**安全加固 → 修今天就坏的体验 → provider 通用化 → 运维能力**

一句话方案：

> 先补上租户隔离与凭据回显，再把「飞书」从代码里拆成服务端 ProviderSpec 注册表，
> 由服务端展平出有序 FieldSpec 交给前端直接渲染；第二个 provider 是钉钉，
> 验收标准是它落地时 `web/` 零改动。

## 2. 文档索引

| 文档 | 内容 | 读者 |
|---|---|---|
| [PROGRESS](PROGRESS.md) | 📌 **进度账本**（维护协议 + 四阶段任务表 + 跨仓联动 + 部署矩阵 + 变更日志） | 全体 |
| [DECISIONS](DECISIONS.md) | `CHN-ADR-NN` 决策记录 | 全体 |
| [CONTRACT](CONTRACT.md) | ⭐ 前后端接口契约与 `channel-api/vN` 版本标记 | 前后端 |
| [`api/channels/README.md`](../../api/channels/README.md) | 已上线行为、部署形态、上游所有权表（**不在本文档集内，勿重复**） | 研发 / 运维 |
| `web:docs/channel-frontend-design.md` | 前端设计稿（ARCH-6） | 前端 |
| `internal/channel-audit-2026-08.md` | 原始审计：复现步骤、上游对照（本地，不入库） | — |

**分层原则**：入库的是**关于我们自己代码的陈述**；留本地的是**漏洞复现步骤与上游/第三方对照分析**。
判断某段内容该放哪，就问一句：这是在说我们的代码，还是在说别人的？

## 3. 当前阶段 · 现在该干什么

**当前阶段**：阶段 U（今日可见缺陷）· **最后更新**：2026-08-05

| 阶段 | 内容 | 状态 |
|---|---|---|
| **PR-0** | 建立账本与契约文档（两仓 docs-only） | ✅ 完成 |
| **S** | 安全加固（S1–S6） | ✅ 完成 |
| **U** | 今日可见缺陷（U1–U7） | ⬜ 未开始 ← **下一步** |
| **P** | Provider 通用化（P1–P5） | ⬜ 未开始 |
| **O** | 运维能力（O1–O8） | ⬜ 未开始 |

下一批（按顺序做，别跳）：

| 顺序 | ID | 一句话 | 在哪 |
|---|---|---|---|
| 1 | CHN-U1 | `_respond` 把 `data=False` 换成 `data={"error_code": ...}`（后端先，前端可独立落地） | `api/apps/restful_apis/chat_channel_api.py:35-65` |
| 2 | CHN-U2 | 前端错误码接线 + providers 失败不再清空整页 | `web:src/pages/settings/channels/index.tsx` |
| 3 | CHN-U3 | 运行时状态词表收紧到服务端的 6 个，测试挪进有门禁的目录 | `web:src/api/channel.ts:56`、`utils.ts:10-13` |
| 4 | CHN-U4~U7 | 表单重置守卫 / invalidate 替代 setQueryData / 提交前 refetch / 下拉服务端搜索 | `web:channel-form-sheet.tsx`、`use-channel-request.ts` |

> 阶段 U 的 U2–U7 全在 web 仓，与后端无顺序依赖，可与 U1 并行。

## 4. 验证命令（两个仓）

### 后端 · MultiRAG

**本机是 Windows，没有 `make`。** AGENTS.md 里的 `make verify` 在这台机器上跑不了，
直接跑下面的命令（Makefile 里 `UV := uv run --no-sync`）：

```powershell
$env:PYTHONUTF8 = "1"          # 必须；否则中文日志/断言会炸编码
uv run --no-sync ruff format .            # 先格式化，否则下一行在 Windows 上先炸行尾
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync lint-imports
uv run --no-sync python scripts/check_async_sync_db.py
uv run --no-sync mypy
uv run --no-sync pytest tests/unit -q
```

Channel 快速回路（提交前仍要跑上面全套）：

```powershell
uv run --no-sync pytest tests/unit -q -k "channel or feishu"
uv run --no-sync ruff check api/channels api/channel_control api/channel_execution api/channel_runtime
```

⚠️ **三个必须知道的坑**：

1. **`tests/unit` 有 6 条先天失败**（2026-08-05 实测 6 failed / 1486 passed / 761.94s，
   全是 Windows 上 bash 渲染配置模板导致的，与 channel 无关）。**逐条测试名记在
   [PROGRESS · 先天失败基线](PROGRESS.md#testsunit-先天失败基线)**——比对的是**失败集合**，
   不是通过数。出现不在名单里的失败才是回归。被你改动的测试文件另外单独跑，要求零失败。
   全量一次 12 分 41 秒，所以日常用下面的 channel 快速回路，提交前才跑全量。
2. **上面这套不覆盖 CI 全部**：CI 另跑 gitleaks 与 smoke。测试里的假凭据必须低于 gitleaks
   的 3.5 香农熵阈值（历史上踩过，见提交 `fd95d0a1`）——用 `cli_aaaaaaaaaaaaaaaa` 这类结构化
   重复占位符，绝不用 base64/hex 形状的随机串。
3. **新建顶层包或改路由后**必须自查导入（smoke 的本地替身，不需要 Milvus/Postgres/MinIO/Redis）：
   ```powershell
   uv run --no-sync python -c "import api.apps, api.channel_control.schemas; print('import ok')"
   ```

### 前端 · web（`D:/project/web`）

```powershell
npm run lint; npm run lint:file-size; npm run lint:typed; npm run typecheck:agent-strict
npm run test:agent-t1; npm run test:design-tokens; npm run test:streaming; npm run test:api
npm run build; npm run check:bundle-size
git diff --exit-code scripts/file-size-baseline.json scripts/bundle-size-budget.json
```

⚠️ **两个坑**：

1. **只有 `src/api/__tests__/*.ts` 能被门禁碰到 channel 代码。**
   `src/pages/settings/channels/**` 下的测试跑在所有 npm script 之外——写了也不跑。
   纯逻辑要放进能被 `test:api` 的 glob 命中的位置；改组件靠人工验证，
   **把验证方式写进账本，别假装有测试**。
2. 最后那行 `git diff --exit-code` 是关键：两个 JSON 都是只许收紧的棘轮，
   有 diff 就说明是改门禁去迁就代码，而不是反过来。

## 5. 跨仓部署顺序（硬规则）

MultiRAG 与 web 是两个仓、两条 CI、两次部署，**不存在跨仓原子 PR**。

> **后端先，且加法优先（additive-first）。**

1. 契约变更必须先在后端以**加法**上线（新增字段 / 新增端点），老前端不受影响。
2. 前端跟上，切到新形状。
3. 删旧形状是**第三次**部署，只能在前端已上线之后。
4. 「后端已上线、前端还没跟上」是**正常且安全**的中间态，必须记在
   [PROGRESS · 跨仓联动](PROGRESS.md#跨仓联动)。
5. 「前端已上线、后端还没上线」**禁止发生**。出现即回滚前端。

**私有 runtime 契约另有更严的规则**（tolerate-then-emit），见 [PROGRESS §私有运行时契约升级规则](PROGRESS.md#私有运行时契约升级规则)。
一句话：`DesiredRuntime` / `RuntimeBindingConfig` / `RuntimeCredential` / `RuntimeReport` /
`DesiredRuntimeList` 全是 `extra="forbid"`，而 supervisor 与 worker 是长驻进程、
**API 部署不会重启它们**，所以每次改动都要拆成两个 PR、中间夹一次运行时部署。

## 6. 硬不变量（破坏之前先写 CHN-ADR）

1. **Secret 只写不读回。** 公开面永不回显密文或明文；新增字段进公开面之前，必须过与
   `_contains_sensitive_key` **同级的子串判定**，不是精确匹配黑名单。
2. **租户维度先于一切资源获取。** 任何 Redis 命名空间、任何 lease、任何缓存 key 都必须含
   租户或 binding 维度，且在凭据校验**之后**才获取。
3. **不得用 `Principal.id` 当租户角色判据。** `UserTenantService.get_role_in_tenant` 在
   `user_id == tenant_id` 时恒返回 `OWNER`——在 channel 路由上做角色校验是无效的，
   授权必须加在 binding target 上，按**目标的归属租户**判角色。
4. **目标、租户、发布版本只从服务端 binding 解析**，不接受 worker 或用户消息覆盖。
5. **单条坏 binding 不得阻塞全局 reconcile**（fail-isolated，不是整体 fail-closed）。
6. **日志脱敏**：不得出现 App Secret、internal token、tenant access token、完整 WebSocket URL、
   原始事件、问题、答案、完整用户/会话/message ID、MCP 参数。
7. **前端不得自建服务端已有的词表**（运行时状态、错误码、provider 字段）。单一真源在
   [CONTRACT](CONTRACT.md)。
8. **不许放宽门禁换绿**（AGENTS.md 核心规则）。修根因，不改棘轮。

## 变更日志

| 日期 | 变更 | 记录人 |
|---|---|---|
| 2026-08-05 | 文档集建立；审计结论收敛为 CHN-S/U/P/O/X 五族 | Claude |
