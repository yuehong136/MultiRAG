# Skills 调整交付状态

2026-10-04。本轮按“Go 跟源码、Python 跟核心行为并可增强”调整，核心及消费者已实现并验证。
按阶段记录，不把后续远程来源或旧实现退役标记完成。
未 push、未部署、未迁移业务库。其他工作线改动保留。
当前合同见 [核心](CORE_CONTRACT.md)、[Python 资产扩展](CONTRACT.md)，
部署和消费方式见 [README](README.md)，后续阶段见 [调整计划](REALIGNMENT.md)。

## 已确认边界

- 双后端独立数据库、对象前缀和索引命名空间，不再共用七表或跨读对方资产。
- 核心每次明确指定已创建、已授权的空间，无隐式 default。Go 在入口加最小校验。
- 固定 `/skill-core`、`/skill-assets` 协议；`/skills` 仅由部署配置指定别名。
- Go 核心保留实体、DAO、服务、handler 和 CLI provider 的来源对应关系；PostgreSQL、
  模型精确身份、原生 Files、Milvus 和必要清理恢复在局部边界适配。
- Python 核心使用私有两表与 Files；七表资产、不可变发布、持久任务、真实 rerank 留作增强。
- 不执行技能，不接 Agent 自动执行。远程来源、自动更新和全空间迁移工具不属于本次实现。
- 普通 Files move/rename 不得绕过核心生命周期；本期明确拒绝受管树操作。

## 本轮提交

| 单元 | 本地提交 | 当前状态 |
|---|---|---|
| CLI 资产固定协议 | `9c186373` | 已提交，消费者不猜测 `/skills` 别名 |
| CLI 核心与共同黑盒合同 | `5360a355` | 已提交，真实 Python/Go HTTP 消费通过 |
| 空查询索引语义回归 | `eadc547e` | 已提交，两端同一黑盒断言通过 |
| Web 资产固定协议 | `fb461b8`（Web 仓） | 已提交 |
| Go 核心与引擎适配 | `e7f3492c` | 已提交，源码主体、隔离存储、大文档和故障恢复验证通过 |
| Python 核心与隔离 | `7fcdc6da` | 已提交，真实核心/资产、故障恢复及worker生命周期通过 |
| Web 核心与体验 | `8ed5d2c`（Web 仓） | 已提交，三种模式真实浏览器与完整CI通过 |
| Python MCP 只读分发 | `ec937885` | 已提交，官方 SDK 真实 HTTP、隔离与完整MCP回归通过 |

## 本轮实际证据

- 选定 Python 核心、故障恢复、资产、搜索、HTTP、MCP、文件批删、DB bootstrap 正式
  `make integration`：**42 passed，0 skip，131.74s**；
  `.test-results/20261004-231508-80408`，日志 `/tmp/skills-realignment-integration.log`。
  评分及公共搜索钩子修正后，受影响核心/恢复/搜索/HTTP/MCP/批删复跑 **14 passed，
  0 skip，133.74s**，`.test-results/20261004-232216-96325`；包括worker实际启动/停止、
  默认threshold0.2的关键词命中及weight0不调用embedding。
- Go 核心独立数据库、真实 SQL/MinIO/Milvus/CLI：最终完整 **7 passed，344.45s**；
  `.test-results/20261004-233519-26170`。覆盖共同合同、失败保留旧索引、文件清理恢复、
  101目录与索引分页、18种缺失/空白空间请求无副作用、5MiB多字节正文/大元数据/全片删除、
  未发布片段不影响旧keyword结果、Put后SQL失败/重启/迟到写入清理。
  删除计划误认新请求、配置SQL失败吞错修正后，新增两项及原删除恢复 **3 passed，53.74s**；
  `.test-results/20261004-234022-27972`。DAO及Search最后SQL错误传播修正专项
  **1 passed，23.34s**，`.test-results/20261004-234221-28784`；255个中文字符标识的
  最后索引字段边界修正 **1 passed，33.77s**，`.test-results/20261004-234435-29139`。
  不同代码时点分开记录。
- `make smoke` 对隔离完整 FastAPI 路由：ping200、healthz200，数据库、Redis、索引、
  存储均 ok；`/tmp/skills-realignment-smoke.log`。该 HTTP fixture 禁用完整应用 lifespan，
  新增 worker 的启动/停止与真实HTTP恢复由上面核心集成独立验证。
- MCP 真实 HTTP + FastMCP 官方 list/manifest/download、中文/二进制、租户拒绝及删除
  后拒绝已纳入上述集成；单独运行证据 `.test-results/20261004-225649-53488`。
  `make mcp-compat` modern/legacy 双方向矩阵全 PASS，日志 `/tmp/skills-mcp-compat.log`。
  Skills与既有MCP入站单测 **19 passed**，其中Skills **5 passed**，补百分号路径与
  `_manifest` 附件冲突明确拒绝。
- 核心空查询保留索引语义：未建索引、DELETE index后返回空，Files目录仍可读。
  Python最新共同合同 **1 passed，80.86s**，`.test-results/20261004-232923-11841`；
  Go最终suite已复验相同脚本。
- Go全部 `internal` build/vet和三个独立main构建通过；engine/server/service/handler/skills
  五个改动包测试通过。全量Go测试仍有既存 `entity.TestModelLevelThinkingAndGoogleFactory`
  MiniMax目录断言失败（model_test.go:236），在无本次Go补丁的 `ec937885` Git归档源码
  独立复现，未修改该无关目录或放宽断言；不能宣称Go全库测试全绿。构建/定向测试日志
  `/tmp/multirag-skills-go-final-checks.log`，独立基线日志保留于本机临时
  `multirag-skills-head-baseline-*/evidence.txt`。
- CLI `go test ./internal/cli/... -count=1`、`go vet ./internal/cli/...` 通过；
  `/tmp/skills-core-cli-final.log`、`/tmp/skills-core-cli-vet.log`。
- 第一轮 `make verify` 为 **5425 passed / 15 failed**：14项普通文件批删测试替身缺新增
  核心识别边界，1项禁止测试间导入。根因已修，原断言和门禁保留。最终 `make verify`
  **exit0、5440 passed（52.62s）**，Ruff、import、async DB门禁及mypy137源文件均通过；
  `/tmp/skills-realignment-verify-final.log`。
- 用户授权的隔离 Playwright Chromium 三种模式全部通过：Python资产增强、Python核心、
  Go核心，页面错误均0；两个核心另验Files未索引目录可浏览。1440明暗主题与390px布局
  两张标注拼图已独立审阅：`/tmp/skills-realign-contact-sheet.jpg`、
  `/tmp/skills-realign-states-contact-sheet.jpg`。安全验收摘要 `/tmp/skills-realign-verification.md`。
  Go通过Vite同源真实反代，未验证跨源CORS。
  消费者预检发现并修正模型列表端点、Go 空默认配置 ID、Files 布尔/批删结果联合合同，
  以及 Python 核心默认关键词评分；不会只以共同脚本通过代替真实 Web 消费。

- Web完整CI **1203 passed**（699 Node、413 DOM、81 desktop、10 tooling）；
  build、产品UI30、体积及i18n通过。lint0 errors、1454存量warning。临时开发服务已停止，
  Vite验收配置已删除，Web工作区干净。中间既有MCP时序测试偶发失败，独立与最终完整
  复跑均通过，未修改该无关测试。
- Python UI fixture已正常退出，端口和私有handoff关闭/删除，已证明归属的索引集合不存在。
  三个早期疑似测试集合无法由留存记录证明归属，未擅自删除；证据
  `.test-results/skills-core-residual-audit-20261004.json` 与 `skills-python-ui-cleanup-20261004.json`。

真实基础设施为隔离 PostgreSQL、MinIO、Milvus；模型 provider 使用本地 HTTP 替身。
该证据不等同于真实付费 provider、生产切换或所有检索引擎认证。
最后Go验收脚本调整后，`make lint`及测试harness单测27项再次通过。

## 尚未实现或未验证的范围

Go 的 Elasticsearch/Infinity 核心适配尚未实现、未注册；显式核心模式拒绝不支持的引擎，
默认资产模式中核心入口明确503。Go核心只保存 rerank 配置，不执行 rerank。
大正文 Milvus 分片、整文逻辑身份与失败可见性已经实测；评分不承诺与其他引擎数值一致。

Python 只读 MCP 默认关闭；原始包如含根 `_manifest` 附件，MCP分发明确拒绝，原始 ZIP
下载仍可用。未升级 FastMCP 依赖，未实现完整 Skills 规范认证、远程来源治理或 Agent 沙箱。

现无技能版本需要历史迁移；本机一个空空间保持原样。未执行跨部署全空间迁移、旧 Go
七表删除或生产数据清理。旧 Go 资产实现仅在核心模式只读并排空既存任务，退役另行处理。

## 前置与历史记录

文件批删正式合同 `bdbe93e0`、`c039e005` 已接收，见[删除合同](../references/file-deletion.md)
及[验收](../ragflow-porting/file-deletion-acceptance.md)。Go 模型前置 `4ba652f3` 已交接，
保持 `model_service.go` 的现有公开签名；新技能绑定在边界适配。

原共享方案的提交为 `c8087791`、`0342420b`、`c2a7d344`、`a940e8a3`、`bc1c9ccd`、
`5419f954`，Web 为 `326f4748`；调整计划为 `eaec4e23`。这些历史测试不能证明本轮收敛完成，
旧共享数据库/跨读方案已被本轮独立部署合同替代。
