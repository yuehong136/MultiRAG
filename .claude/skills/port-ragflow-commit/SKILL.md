---
name: port-ragflow-commit
description: 跟进/移植一个 ragflow 上游 commit 到本项目。当用户给出 ragflow commit hash 或 PR 号要求跟进、移植、对齐上游时使用。涵盖基准选择、栈判定决策树、框架差异映射、验证与提交约定、判例库。
---

# port-ragflow-commit：ragflow 提交跟进流程

本项目持续逐 commit 跟进 ragflow。默认上游 checkout 是 MultiRAG 仓库的兄弟目录
`../ragflow`；不同机器可用 `RAGFLOW_REPO` 显式覆盖。本 skill 是该工作流的权威流程；判例随每次
跟进沉淀在文末判例库与记忆 `project-ragflow-followup`。

## 0. 参考基准（先做，不可跳过）

```bash
MULTIRAG_REPO="$(git rev-parse --show-toplevel)"
RAGFLOW_REPO="${RAGFLOW_REPO:-$(dirname "$MULTIRAG_REPO")/ragflow}"
git -C "$RAGFLOW_REPO" fetch origin
```

**一律以 `origin/main` 为基准。**
本地 `main` 与工作树可能落后、领先、含被上游重写的历史、合并或未提交改动；这些都是每台机器、
每个时点不同的状态，必须在当次任务实时检查，**绝不能拿本地 main / 工作树当上游基准**。取某提交
当时的文件版本用 `git -C "$RAGFLOW_REPO" show <commit>:<path>`（HEAD 可能已演进，cp 工作树会
取错——判例 #13887）。

## 1. 输入

ragflow commit hash / PR 号。动手前的固定体检：

1. `git show --stat <commit>`——**多主题 PR 必须看完整足迹**，别只做 headline（判例 #13717：漏了鉴权/admin/model 层被用户两次点出）。
2. **查 revert / re-land / 后续修复链**：该 commit 是否被 revert？是否有 re-land + 修复串？以 re-land 链为基准（判例 #13690：`1db5409d` 被 revert，基准是 `4bb1acaa5` + 三个修复）。
3. **grep 目标接口在我方是否已存在**（尤其 `sdk/` 下）——避免路由撞车或重复实现（判例 #13741：`/api/v1/files` 我方早有，差点并排新建抢路由）。
4. 契约是否稳定：该模块从 commit 到上游 HEAD 是否又演进（演进了则评估直接对齐 HEAD 形态还是忠实本 commit——默认**只做本 commit 做的事**，判例 #13983）。

## 2. 评估决策树

### 2.1 该 commit 动了什么栈？

| 栈 | 处置 |
|---|---|
| **Python 生产栈**（api/、core/ ⇔ 上游 rag/、common/ 等） | 默认跟进对象。废弃上游已删/注释的接口时**不删不注释**：`deprecated=True` + 注释横幅留旧（生产在用） |
| **Go 停滞港**（cmd/、internal/，≈192 个 .go） | 按需跟（用户点名或 Go 相关提交）。**不留旧别名，直接删旧对齐 ragflow 该 commit 的最新形态**——deprecated 保留约定只适用 Python 生产栈 |
| **前端**（上游 web/） | 独立仓 `/Users/dxl/project/ts/web`，**一律单独排期**（API 契约变更也单排，除非用户点名同批）。我方是 React 19 + react-query + 自研 apiClient 从零重写，**永不照搬 ragflow web 代码**：契约级改动映射到我方 `api/*.ts` 且**参照 ragflow web HEAD 契约**（历史前端快照无参考价值） |
| **文档/CI/工具链** | 逐项评估；上游 harness（lefthook、.agents/）不跟，我方已有等价物 |

### 2.2 跳过判例库（满足任一即评估跳过，向用户说明理由）

- **WIP 不编译**（#13948 调用未定义函数 + 调试残留）；
- **对我方是功能回退**（#13948/#13952：把我方可用的 contextengine 真实现降级成空桩）;
- **上游已废弃该路线**（#13948 系被上游 #15838 整体删除重做——追已死的中间链不如等终态）；
- **半成品 bug**（#13955：lexer 标了 TokenFloat 但 parseFloat 仍拒收，照搬引入 bug）；
- **上游在追赶我方已完成的东西**（#13831 的 CLI 大块 diff = 上游补我方早已落地的拆分/contextengine → 只做真正新增）；
- **ragflow 自身 py/go 就有分歧的行为**（#13956 owner_ids：query vs body）→ 我方忠实镜像各自形态、不自行统一；
- **上游后续已撤销的短命改动**（#13922-C 的复合唯一索引被 #15460 移除 → 不制造短命迁移）。

### 2.3 「真正的行为修复」vs「顺带的重构」（Python 侧核心判据）

跟进一个 commit 前先把 diff 拆成两类，分别处理：

- **真正的修复/新行为**（我方缺失）→ **逐字照搬、保留 ragflow 方法名**，便于后续对齐（判例 7827f0fc 的 `_is_truncated_cache`）。
- **顺带的重构**（上游为达到某效果做的清理，而我方早有更好/带类型的等价物）→ **不照搬，映射到我方既有抽象**（判例 7827f0fc：继续用 `unwrap_graphrag_chat_response`；我方刻意建立的 Protocol/TypeAlias/helper 与「FastAPI 而非 Quart」同属有意分歧）。
- 我方与上游改动前逐字对应、无抽象分歧的领域 → 行为+重命名都忠实照搬（判例 d32967ed excel LazyImage）。

## 3. 移植步骤

读上游 diff → 映射到我方文件 → 适配框架差异 → 落地。

### 3.1 Python 框架差异映射

| 上游 | 我方 |
|---|---|
| Quart/Flask `@manager.route`、`request.get_json()` | FastAPI `APIRouter` + Pydantic body + `Depends` |
| peewee 查询 | SQLAlchemy 2.0 `select().where()`，`db: Session` 依赖注入 |
| `api/settings.py`、`rag/settings.py` 本体改动 | **查表翻译**：`docs/enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md`。消费者侧 `settings.X` 用法 diff **照抄**（`common/settings.py` 是永久兼容 facade） |
| `rag/` 目录 | `core/`（如 `rag/llm/chat_model.py` ↔ `core/llm/chat.py`） |
| `api/apps/restful_apis/*_api.py` | 我方同名文件 |
| `api/apps/services/`（路由层服务） | 落位须过 import-linter「api 服务层不依赖路由层」契约；映射决策记入 porting map |
| `backward_compat.py`（旧路由集中兼容层） | RESTful 移植时采纳该模式，替代散落各文件的 deprecated 别名 |
| llm_factories 的 `model_type` | 我方 `mdl_type` |

### 3.2 async 规则（⚠ 每个 handler 都要过一遍）

上游已全面 Quart 化：handler 全是 `async def` + 同步 peewee——**这是事件循环阻塞病灶，照抄会回灌**（我方曾 32 文件中招，门禁 `scripts/check_async_sync_db.py` 常驻 `make lint`，基线只减不增）。

- **默认**：上游 `async def` handler 移植到 FastAPI **降为普通 `def`**（自动进线程池）。
- 函数体有**真实 await**（SSE 流式、LLM 异步调用）才保留 `async def`，且同步 DB 读写收拢到流式前后或 `run_in_threadpool` 包裹。
- **日落条款（async 基建 Phase 0 落地后生效）**：新移植的 restful service **一律 async-first**——`async def` handler + `db: AsyncSession = Depends(get_async_db)`，不再降级；同一请求内**禁止混用**同步/异步两种 session。规范表见 AGENTS.md「异步 SQLAlchemy 编码规范」。

### 3.3 测试

**测试不做 1:1 移植**。只移植产品代码；配套测试按我方三形态（TestClient 契约式 / 纯函数打桩 / 真库 scratch，选型表见 AGENTS.md「写测试」）重写，勿照抄上游测试结构。

### 3.4 Go 侧适配速查

命名遵循上游 `.agents/skills/go-naming/SKILL.md`（直接读原文）。固定映射：

- import `ragflow/` → `multirag/`；命名 `ragflow_*` → `multirag_*`；token 前缀 `multirag-`；配置目录 `conf/` → `configs/`；`internal/util/` → `internal/utility/`（有 `GetProjectBaseDirectory()`，勿新建 path helper）。
- 客户端 receiver 是 **`MultiRAGClient`**（非 `RAGFlowClient`）；HTTP `Response` **无 `Duration` 字段** → exec 末尾 `result.Duration = 0`；HTTPClient 字段 **`APIKey`**（非 `APIToken`）。
- handler 用我方 `GetUser(c)` / `common.Code*` / `jsonError`/`jsonResponse`（非裸 `c.Get("user")` + `c.JSON(gin.H{...})`）。
- CLI：共享叶子 parse 函数放 `parser.go`（不复制进 admin_parser/user_parser 双份）；`expectSemicolon` 必分号；输出格式用 `-o`/`\format` 既有设施；模式路由是**反向 `looksLikeContextEngine`**（仅 ls/cat/search 走 CE），上游给 `looksLikeSQL` 加前缀对我方 N/A；新命令四步：types/lexer 加 token（注意 `isKeyword` 上界）→ parser 分发 → command 执行函数返回 `ResponseIf` → client 的 Execute 分流接线。
- 接口签名变更时**我方多一个 milvus 引擎**（上游无），必须三引擎（infinity/elasticsearch/milvus）同步改；milvus 本就是桩，新方法补桩即可。
- Infinity SDK 版本跟 ragflow：bump 到目标 commit 所用版本（非上游 HEAD）。
- **绝不做「返回成功却不写入」的假 stub**（比缺功能更糟，判例 #13928 SetMeta 的处置）。

## 4. 验证

- `make verify` 必绿（任何落地的底线）；
- 动 DB/存储/检索 → 加 `make integration`；改启动/路由/健康检查 → `make smoke` 冷启动验证；
- Go 侧：`go build ./internal/...` + `go vet` + 三个 cmd main 各 `go build -o /dev/null cmd/<x>.go` 单独编译（`main redeclared`、`llm.go:355` vet 告警是预存在噪音）；gofmt 只看新增行（存量 import 乱序预存在）；
- CLI 语法改动：临时 parser 测试跑过后删除；能 live 实测则 live 实测。

## 5. 提交

- conventional commit；**不提 ragflow/上游来源；无 Co-Authored-By 尾注**；
- **`internal/*.md` 绝不 git add**（本地笔记）；
- 惯例：Go 一个 commit、Python 一个 commit 分开提；
- 工作区常有用户自己的未提交改动：用「hunk 内容判据」（含本次关键词的 hunk = 我加的）`git apply --cached` 分离，只 add 自己动过的文件。

## 6. 收尾

1. 更新记忆 `project-ragflow-followup`：当前跟进位置、backlog、新判例（含跳过判例与理由）；
2. 发现上游**结构性变化**（目录重组、框架迁移、门禁工具变更、nginx hybrid 默认档位变化）→ 补 `docs/enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md` 与方案文档 §10 各一行；
3. **观察哨**：留意上游 nginx conf 默认 scheme 是否翻 `go`、Python 侧 commit 密度是否骤降（Python 进维护模式 = 跟进经济学根本改变，届时向用户报告重开决策）。

## 附：判例库（按教训索引）

| 判例 | 教训 |
|---|---|
| 7827f0fc（mind map fix） | 「真修复逐字搬 / 顺带重构映射我方抽象」二分法的确立 |
| d32967ed（excel LazyImage） | 无抽象分歧的领域 → 行为+改名全忠实照搬 |
| dd839f30（matplotlib/tool-calling） | 文件映射要核实（chat.py 非 chat_model/base.py）；重 service 顶层 import 触发循环导入 → 惰性 import |
| #13741（files RESTful） | **动手前先 grep 目标接口是否已存在**；路由撞车时收编为唯一入口 + 墓碑退役 |
| #13717（b308cd3a，Go token） | 多主题 PR 看 `--stat` 完整足迹；鉴权链最易漏但最关键 |
| #13765（Go admin tokens） | 上游 PR 描述与实际代码不符时以代码为准；我方已修对的 bug（LIST 带 user 过滤）保留不回退 |
| #13776（contextengine） | 先确认上游契约到 HEAD 是否稳定；与我方既有 SQL 命令冲突要消歧；用户要求后可反转「跳过死代码」决定全量对齐 |
| #13831/e20cf397（model pool） | 上游大块 diff 可能是在补我方已完成的东西——只做真正新增 |
| #13887（provider 五表） | 上游「两套并存」可能是多提交迁移的中间态而非我方跟岔——`git ls-tree <commit>` 验证；合并点（#14398）到了再删 |
| #13903（Go upload） | 上游 Go bug（路径解析）→ 对齐我方 Python 语义修掉；照搬保留与顺手修要向用户逐项确认 |
| #13928 + #13706 回补 | 缺基盘的功能先补基盘再开写侧；绝不做假 stub；旧注释变假要顺手修正 |
| #13948/#13952/#13955 | 跳过判例四型：WIP 不编译 / 对我方回退 / 上游已废弃路线 / 半成品 bug |
| #13983（log level CLI） | 「只做本 commit 做的事」判据：它删的我删、它留的我留；Go 侧值级适配（我方 config 更丰富）非漂移债 |
| #13956（version RESTful） | Python 加新留旧（deprecated），Go 直接对齐；ragflow 自身 py/go 分歧则不跟 |
| #13974（Go Delete + 表命令改名） | ① 上游大拆文件（index.go→common/dataset/metadata）：先 `git show pre/post` 逐函数 diff 隔离**真实改动**，再按上游结构**重排我方文件**只套真实改动——保住本地质量改进（路径 helper/注释/typing），git 还能识别 rename；② 上游 rename 半途遗留的死函数副本（旧函数改名后又新建同体函数）→ 验 origin/main HEAD 已消亡即不搬；③ 上游新增却无任何规则引用的 lark 终结符（DATASET_TABLE）不搬（标准 lexer 下反而抢 token）；④ 上游 transformer 把 doc_id 误收进 chunk_ids → 用我方既有 remove_tags 的 FROM 截断惯用法修正（#13903 谱系：bug 不照搬，修法贴我方惯用法） |
| 鉴权统一（_load_user 对齐） | fastapi_login 兜底要重写 `__call__` 整体（不是 get_current_user）；改 `__init__.py` 须重启服务生效 |
| #13972（ES9 dense_vector fields） | **上游过渡期洞**：commit 主目的是终态（HEAD 存活）但携带副作用回归（get_fields 丢 `_score`，Dealer/KG 打分静默归零）、上游 6 周后在 #14970 改调用方绕过而非回修 → 结构照搬 + 最小垫片保我方契约 + **记日落点**（跟到 #14970 删垫片收敛），垫片配钉板测试变异验证防未来"忠实对齐"误删；评估时先查该 commit 到 HEAD 的同文件演进链，"后续修复链"可能改的是调用方 |
