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

**当前阶段**：24 个 PR **全部落地**，第二个 provider（钉钉）已注册，排期外追加落地
CHN-O7（主密钥密钥环）、CHN-O12（空 env 变量）、CHN-O6（连接自检）·
**部署**：CHN-P11 的两道闸门均已完成（2026-08-06 14:44 重启 API 与 supervisor）·
**无待办** · **最后更新**：2026-08-06

| 阶段 | 内容 | 状态 |
|---|---|---|
| **PR-0** | 建立账本与契约文档（两仓 docs-only） | ✅ 完成 |
| **S** | 安全加固（S1–S6） | ✅ 完成 |
| **U** | 今日可见缺陷（U1–U7） | ✅ 完成 |
| **P** | Provider 通用化（P1–P11、P13 全部完成并部署） | ✅ 完成 |
| **O** | 运维（O1–O7、O12 完成；O8–O11 未排期） | ✅ 完成（排期内） |
| **X** | 跨仓契约（X1–X3 完成） | ✅ 完成 |

### 24 个 PR 全部落地并部署完毕

**CHN-P11 是最后一条**（删 `RuntimeCredential.app_id/app_secret`，即「删字段三步」的
第三步：① 停止读 CHN-P4 ② 停止发 CHN-P8 ③ 删除 CHN-P11）。它有两道闸门，都已关闭。

闸门①**不是等够了时间，是被证实了**：CHN-P8 提交 `09:42:26`，本机 supervisor `10:54:57`、
worker `10:55:01`、API `11:15:01`，全部晚于提交一小时以上；用户确认目前只有这一台机器。
浸泡期防的是「你数不清的 runner」，这里能数清，所以它的目的已经达成。

**闸门②也已完成（2026-08-06 14:41–14:44，用户批准后执行）**，顺序是先 API 后 supervisor——
反过来会让新 worker 先起来撞上还在发 legacy 的旧 API。实测结果：

| 进程 | 起于 | 证据 |
|---|---|---|
| API | 14:41:03 | `POST /api/v1/chat-channels/x/verify` 返回 **401 而非 404**，说明 CHN-O6 的路由在册 = 新构建 |
| supervisor | 14:44:04 | `worker_started result=ok` |
| worker | 14:44:07 | `ws_connected result=ok`；DB 里新 runner `vm-duxiaolong-34692`、`connected`、`last_error_code` 为空 |

**这就是 P11 的决定性验证**：一个 P11 worker 解析了一个 P11 API 发来的载荷并连上了，
日志里搜不到任何 `RUNTIME_CONFIG_INVALID` / `validation error` / `extra_forbidden`。
Redis `multirag:channel:v2:*:leader:*` 键已回来。**已知代价**（与 CHN-S3 同类）：
飞书会话重置了一次、dedupe 窗口空了一次。

### 新增第二个 provider 时的独立闸门（与契约版本无关）

`supervisor.py:35` 的 `_SUPPORTED_PROVIDERS` 是 **import 时冻结的模块级常量**，所以
**注册任何新 provider 都要重启一次 supervisor**。好在它 fail-safe：不认识的 provider 走
`:128` 的 `provider_unsupported` **逐条跳过**，健康的 binding 照常 reconcile。

> **2026-08-06 复核：曾经写在这里的「supervisor 早于钉钉注册、会跳过钉钉 binding」
> 已不成立，别照着做。** 当前 supervisor 起于 **14:44:04**（P11 部署那次重启），
> CHN-P10（注册钉钉）提交于 **10:46:30**——**进程远晚于注册，认识钉钉**。
> 建第一个钉钉渠道**不需要**为此重启 supervisor。
>
> 这句话会反复过期，所以记录的是**怎么重新判定**而不是结论：把下面查到的进程
> `CreationDate` 与 `git log --grep='CHN-P10' --format=%cd --date=iso-local` 比一下，
> 进程晚于提交就认识。以后每注册一个新 provider 都要重判一次。

### ⚠️ 关于「部署闸门」：先查，别假设

上一轮我按计划文本假设「所有 emit 半步都卡在部署上」，**查了之后有三条并不卡**。查法：

```bash
docker ps -a                                     # 有没有 supervisor 容器
docker exec multirag-redis valkey-cli -n 1 --scan --pattern 'multirag:channel*'
docker exec multirag-postgres psql -U usr_ai -d postgres \
  -c "select runner_id, state, heartbeat_at from t_ai_channel_runtime_status"
```

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CreationDate, CommandLine |
  Where-Object { $_.CommandLine -match 'api.channels' }
```

把进程 `CreationDate` 与 tolerate 提交的 `git show -s --format=%cd` 对比即可。
**关键事实**：worker 是 supervisor 每次 spawn 的**全新解释器**，按盘上代码加载，所以它
往往比 supervisor 新得多——2026-08-06 实测 supervisor 起于 8/5 09:44（旧代码），
worker 起于 8/6 08:37（已含 CHN-P4 与 CHN-O2）。**`RuntimeBindingConfig` 的闸门由
worker 决定，`DesiredRuntimeList` 的闸门由 supervisor 决定，两者不是一回事。**

解除 supervisor 闸门（**会重置一次会话、清空一次 dedupe 窗口，先问用户**）：

```powershell
# 整棵树一起杀：uv.exe -> python shim -> supervisor -> worker shim -> worker
taskkill /PID <uv.exe 的 PID> /T /F
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',
  '-File','D:\project\MultiRAG\scripts\run_channel_supervisor.example.ps1' `
  -WorkingDirectory 'D:\project\MultiRAG' -WindowStyle Hidden `
  -RedirectStandardOutput 'D:\project\MultiRAG\logs\channel-supervisor.log' `
  -RedirectStandardError  'D:\project\MultiRAG\logs\channel-supervisor.err.log'
```

> ⚠️ **必须走那个启动脚本，不能直接 `uv run python -m api.channels.supervisor`。**
> supervisor 的两个必填项（`RUNTIME_API_BASE_URL` / `INTERNAL_API_TOKEN`）**不在
> `configs/*.yaml` 里**——`service_conf.yaml` 与 `local.service_conf.yaml` 都没有
> `channels:` 段。脚本从 `%LOCALAPPDATA%\MultiRAG\secrets\supervisor.env` 读它们
> （该文件刻意不含主加密密钥，脚本还会主动拒绝带着密钥启动）。直接 `uv run` 起来的进程
> 环境是空的，会立刻 `error_code=CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED` 退出。
>
> 日志走 stderr（logging 默认），`channel-supervisor.log` 通常是空的，**看 `.err.log`**。
> 起来后确认这三样：`ws_connected` + `worker_started result=ok`；
> `t_ai_channel_runtime_status` 出现新 `runner_id` 与新鲜心跳；
> Redis `multirag:channel:v2:*:leader:*` 键回来了。

## 3.5 怎么把一条任务派给「没有任何上下文的我」

一句话版本：**说 ID，别说需求。**

```
读 docs/channel-program/README.md，然后做 CHN-O7。
```

这就够了。README 是入口，它指向 PROGRESS.md 的任务表与「待办任务简报」，简报里有问题
描述、证据、闸门、验收标准和从哪读起；DECISIONS.md 有为什么是这个方案；CONTRACT.md 有
前后端契约。**不需要你复述背景**——复述反而危险，因为你记得的是几周前的状态，而文档是
按维护协议持续更新的。

几条让交接不出错的补充：

- **一次只派一条。** 这些任务有闸门依赖，同时开两条容易在半态上打架。
- **想让它先确认再动手**，就加一句：`先复核锚点，把你要改的文件和验收标准说给我听，
  我确认后再写代码。` 简报里的行号一定会漂，维护协议第 1 条要求先核对。
- **涉及重启线上进程的**（CHN-P11 要重启 API、注册新 provider 要重启 supervisor），
  加一句：`重启前先问我。` 重启会让飞书 bot 会话重置一次、dedupe 窗口空一次。
- **不确定该派哪条**，就说：`读 docs/channel-program/README.md，告诉我现在最该做什么、
  为什么。` §3 已经按「闸门是否满足」排好了。

反面例子（**不要这样写**）：

> 我们之前做了一个 channel 的重构，有个凭据加密的问题，你帮我看看能不能支持密钥轮换……

这样写会让我从零重新调研一遍已经调研过的东西，而且大概率得出与 `CHN-ADR-06` 冲突的方案。
ID 是这套账本存在的全部理由。

**验收也用 ID 收口**：完工后让我按维护协议更新 `PROGRESS.md` 状态 + 变更日志，并把
`CHN-<面><n>` 写进提交标题——`git log --grep=CHN-` 是这套账本唯一的交叉校验手段。

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
| 2026-08-06 | CHN-O7 落地（主密钥有序密钥环，轮换不再等于全量凭据丢失）；追加并落地 CHN-O12（留空的 env 变量不再打死配置加载——默认 docker 部署原本起不来）；CHN-O6 落地（连接自检端点，后端已上线、前端未接，契约见 CONTRACT §1）。§3 阶段表随之更新 | Claude |
| 2026-08-06 | CHN-P11 落地（删掉每个 provider 都得共用的飞书字段，删字段三步走完）——**但 API 重启未做，那是个现存的故障窗口，见 §3**；CHN-O13 落地（自检的前端接线，web `ea0e5af`）。至此 24 个 PR 全部完成 | Claude |
| 2026-08-06 | CHN-P11 部署完成（重启 API 与 supervisor，先后顺序有讲究，见 §3）。至此 24 个 PR 全部落地**且全部部署**，无待办 | Claude |
