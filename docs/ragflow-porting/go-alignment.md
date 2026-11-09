# Go 结构与合同收敛

本专项的基线是 `1f154bea3e60b378caecb3b2fdf9e1e672ca7f3d`。生产后端仍使用
Python；Go 尚未部署，本次收敛不保留旧 Go API、别名、包装器或旧数据布局的兼容承诺。
不切换生产队列，不迁移 Python 数据，不 push 或部署。

## 实施准则

函数、文件、目录、模块、依赖、配置默认值和删除项都属于对齐范围。保持直接可查的路径和
符号对应关系，不为局部实现自行拆文件、细化函数或发明替代抽象。项目名和 import 的机械映射
单列说明。必要缺陷修复在原位置完成；提前采用后续改进必须有明确提交依据，记录范围和原因。

“已评估”“部分实现”“结构不同”“目标自身未完成”和“已验证对齐”分别记录。
文本相同、路径覆盖、旧批次完成记录、已有本地测试通过，都不能代替合同和运行验证。
本地测试随合同迁移：删除只服务于已退役兼容入口的测试，保留实质失败、授权、并发和副作用
断言，并迁到对应文件。不能通过放宽断言或删除有效回归换取通过。

## 依赖与完成条件

| 事项 | 当前事实与收敛动作 | 依赖 / 调用方 | 必须取得的证据 | 状态 |
| --- | --- | --- | --- | --- |
| GOA-00 全树和历史审计 | 建立逐路径、逐提交及全部删除足迹账本；逐模块比较活动合同与原结构 | 全部 Go、native、构建、配置、文档和跨仓调用 | 语义处置、删除去向、例外来源；计数只用于查漏 | 结构盘点完成，语义审计进行中 |
| GOA-01 启动、配置与 SQL | 将 Go 配置和数据库初始化收敛到独立 MySQL 环境；去掉 PostgreSQL 分支；迁移 DAO、schema 与状态字段 | `internal/server`、`internal/dao`、`internal/entity`、各 cmd；Python 配置/数据库只读查界 | scratch MySQL 建表、事务提交与失败读回；确认不读取生产 Python 配置 | 待实施 |
| GOA-02 模型 | 模型定义回归对应目录；typed `ListModels` 和公共转换下沉到 driver；统一 messages 流接口；实例构造、媒体接口、provider 身份及配置一致；删除旧包装/投影 | 全部 provider、service、handler、agent 和 CLI；不得用旧测试阻挡签名迁移 | 受控 provider、元数据/索引向量、取消和错误；真实 HTTP/CLI 与 SQL 读回 | 待实施 |
| GOA-03 解析和 native | 默认 Office native，补齐对应文件和 PDF；移除内置解析替代路线；native 构建和字典与实际平台一致 | C++ binding、tokenizer、parser、deepdoc、CLI | 各真实格式、失败/空内容边界、native 编译链接、真实 CLI | 待实施 |
| GOA-04 ingestion / 队列 | 补齐 ingestion task/tasklet 与日志、控制面 API、NATS、独立 worker；DSL 去掉本地额外 stage 限制；逐项标记执行器占位 | GOA-01、GOA-03；engine、DAO、admin、router、CLI、worker | 真 NATS 传输、ACK/NAK、故障重投、取消/停止/删除、独立 SQL 读回；模拟执行不能报成功 | 待实施 |
| GOA-05 Agent | 文件/函数和 DSL 结构收敛；补 session 创建；恢复 create/publish/run 参数与默认值；占位组件与真实 CRUD 分开处理 | GOA-01、GOA-02；canvas、组件、runtime、handler/router、SQL 与对象存储 | Create→publish→run/version→session HTTP/SQL；故障副作用、取消和 DSL 合法例 | 待实施 |
| GOA-06 检索与存储 | engine 接口、实现文件、过滤/排序和存储生命周期收敛；删除没有目标合同依据的分支；必要缺陷修复逐项保留在原位置 | GOA-01、GOA-02；service/handler/CLI | 隔离索引/SQL/对象存储的实际读写、删除失败与重试、租户隔离 | 待实施 |
| GOA-07 API / CLI / admin | 按对应文件合并本地拆分；同步路由、请求、响应、命令语法、heartbeat、OAuth、skill 和消息合同；删除旧 facade/fallback | GOA-01—06；真实消费者逐项迁移 | 注册路由鉴权、真实 CLI/HTTP、admin/worker 联动及失败读回 | 待实施 |
| GOA-08 构建、CI 和文档 | 更新独立入口、默认 native 准备和专项登记；删除过期能力说明、失效测试名及旧结构指引 | GOA-01—07；build、CI、开发文档与程序映射 | 锁定组合 build/vet、全部独立 main、race/native/SQL/NATS/HTTP/CLI；本地 CI 配置不称远程运行 | 待实施 |

同一组签名变更与全部源码调用方一起迁移，不能留下永久适配层。先取得定向回归，再锁定组合
集中验收，不为每个中间 commit 重复全套。缺服务是未完成验收，不能转换成 skip。

## 本地证据与当前限制

本地审计和完整对照账本保存在
`/Users/xldu/.local/state/multirag/validation/20261109-go-full-alignment/`，不入库第三方逐行分析：
`reservation.json`、`initial-status.bin`、`initial.diff`、`path-ledger.json`、
`alignment-ledger.md`、`commit-ledger.json`、`upstream-deletions.txt`、
`local-only-deletion-plan.txt` 和冻结目标的 `target/`。

首版盘点包括 1,518 个路径记录、387 个 first-parent Go/构建相关历史提交足迹。
139 个目标路径在简单路径映射后缺失，677 个本地路径无同路径目标；其中包含拆分、搬迁和
测试差异，不能直接称为 139 个缺失能力或 677 个应无条件删除的文件。
208 项只完成机械文本一致性检查；494 项仍存在文本或结构差异。当前没有全树运行对齐结论。

AST 审计另保存 `ast-inventory.json` 和 `symbol-ledger.json`，逐项记录类型、签名、位置、
函数体摘要和直接调用表达式。相同符号有 321 项位于不同文件；这用于发现结构搬迁，
不是已完成语义审计的证明。方法继承、重命名、参数名和 import alias 都需人工解释。

当前首处实现为 `internal/ingestion/chunk_engine.go`：公开 `ChunkPlan.Operators`，按声明
顺序保留全部 stage，取消固定数量、顺序、去重和必选 split 的额外限制；空 pipeline 产生
空 plan。已有 nil-plan 错误、缺失 preprocess 时输入初始化和最终 chunk 索引/Unicode
size 归一化暂保留为有具体问题依据的修复，仍需在整个 operator 链审计中复核。
`go test -race -count=1 -json ./internal/ingestion` 通过（11 个 test/subtest 事件、无 skip），
原始报告和 JUnit 为 `19b40d3a055049c18d12d589d7831014.output` 与
`chunk-dsl-race.junit.xml`。该证据不覆盖 CLI、native、SQL 或队列。

主树 28 项既有 WIP 已留存快照，专项在独立 worktree 实施。所有本专项提交仅暂存已核归属
路径；串行合并本地 main 前重查主树。测试经 `scripts/run_registered.py`，资源先登记、后创建，
保留原始退出、首败、JUnit/JSON 及独立清理读回。真实供应商、生产和远程 `ci-required`
均未运行，不能从受控服务或本地结果推断通过。
